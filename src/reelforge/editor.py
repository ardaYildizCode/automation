"""Shared rendering primitives.

Probing, the Reels-safe envelope (1080x1920, 30fps, H.264 yuv420p, AAC stereo,
faststart) and the canvas-fitting filters every treatment builds on.
Composition itself lives in `compositor.py`.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30
PHOTO_DURATION = 7.0
# Headroom for the still-photo push-in. Just enough to cover PHOTO_ZOOM
# without paying to filter a full-resolution phone photo every frame.
PHOTO_CANVAS_SCALE = 1.25
PHOTO_ZOOM = 0.12

# Finishing pass. CRF 19 at `medium` is meaningfully cleaner than a fast
# preset once Instagram re-encodes on top of it, and the extra runner minutes
# are cheap next to the ad spend behind the winner.
ENCODE_CRF = 19
ENCODE_PRESET = "medium"
UNSHARP_AMOUNT = 0.6
FADE_SECONDS = 0.25
# Instagram normalises loudness to roughly -14 LUFS; matching it up front
# avoids the pumping that its own normaliser introduces.
AUDIO_FINISH = "loudnorm=I=-14:TP=-1.5:LRA=11"

# Instagram's own UI covers the top and bottom of a reel. Text outside this
# band gets hidden behind the caption, the action rail, or the profile row.
SAFE_TOP = 0.16
MIN_REEL_SECONDS = 3.0
MAX_REEL_SECONDS = 90.0
# Trimming to exactly MIN_REEL_SECONDS lands fractionally under it once frames
# are quantised, and Instagram rejects the upload. Aim slightly above the line.
DURATION_SAFETY_MARGIN = 0.35
SAFE_MIN_SECONDS = MIN_REEL_SECONDS + DURATION_SAFETY_MARGIN

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


class RenderError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool

    @property
    def is_portrait(self) -> bool:
        return self.height >= self.width


def ensure_ffmpeg() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RenderError(
                f"{binary} not found on PATH. GitHub Actions ubuntu runners ship it; "
                "locally install it with `sudo apt install ffmpeg` or `brew install ffmpeg`."
            )


def probe(path: Path) -> MediaInfo:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RenderError(f"ffprobe failed for {path.name}: {result.stderr[:400]}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RenderError(f"{path.name} has no video stream")

    try:
        duration = float(data.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0

    width = int(video.get("width", 0))
    height = int(video.get("height", 0))

    # Phones record landscape and carry a rotation flag. ffmpeg auto-rotates on
    # decode, so report the dimensions as they will actually be filtered --
    # otherwise a portrait clip looks like a landscape one here.
    if _rotation_degrees(video) % 180 == 90:
        width, height = height, width

    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _rotation_degrees(video: dict) -> int:
    for entry in video.get("side_data_list") or []:
        if "rotation" in entry:
            try:
                return abs(int(float(entry["rotation"])))
            except (TypeError, ValueError):
                continue
    try:
        return abs(int(float(video.get("tags", {}).get("rotate", 0))))
    except (TypeError, ValueError):
        return 0


def find_font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


@lru_cache(maxsize=1)
def has_drawtext() -> bool:
    """Whether this ffmpeg was built with drawtext (needs libfreetype).

    Static builds routinely omit it. Checking up front lets the text variant
    degrade to a plain render instead of failing the whole batch.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and " drawtext " in result.stdout


def _fit_to_canvas(label_in: str, label_out: str) -> str:
    """Fit any aspect ratio into 1080x1920 over a blurred fill of itself.

    Black bars read as low-effort on Reels; a blurred backdrop keeps the frame
    full-bleed regardless of what Arda shot on.
    """
    return (
        f"[{label_in}]split=2[bg_{label_out}][fg_{label_out}];"
        f"[bg_{label_out}]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},gblur=sigma=28[bgb_{label_out}];"
        f"[fg_{label_out}]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fgs_{label_out}];"
        f"[bgb_{label_out}][fgs_{label_out}]overlay=(W-w)/2:(H-h)/2,"
        f"setsar=1,fps={FPS}[{label_out}]"
    )


def _ken_burns(label_in: str, label_out: str) -> str:
    """Give a still photo motion, so it reads as a reel rather than a slide.

    Uses a time-varying centre crop rather than `zoompan`: zoompan's cost
    scales with the *source* resolution, and on a phone photo it burned
    minutes of CPU per variant. Cropping a modestly oversized canvas produces
    the same push-in for a fraction of the work.
    """
    canvas_w, canvas_h = int(WIDTH * PHOTO_CANVAS_SCALE), int(HEIGHT * PHOTO_CANVAS_SCALE)
    return (
        f"[{label_in}]scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
        f"crop={canvas_w}:{canvas_h},"
        f"crop=w='floor({canvas_w}/(1+{PHOTO_ZOOM}*min(t/{PHOTO_DURATION},1))/2)*2':"
        f"h='floor({canvas_h}/(1+{PHOTO_ZOOM}*min(t/{PHOTO_DURATION},1))/2)*2':"
        f"x='(iw-ow)/2':y='(ih-oh)/2',"
        f"scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[{label_out}]"
    )


def _finish(duration: float) -> str:
    """Common finishing pass applied to every variant.

    Identical across all ten, so it never becomes a confound in the test: it
    lifts the floor on all of them equally rather than favouring one.

    - `unsharp` restores the micro-contrast that scaling softens, which is what
      makes fabric texture and stitching read on a phone screen.
    - A short fade top and tail stops the hard frame-one cut that reads as raw
      camera-roll footage.
    """
    fade_out_start = max(duration - FADE_SECONDS, 0.0)
    return (
        f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={UNSHARP_AMOUNT},"
        f"fade=t=in:st=0:d={FADE_SECONDS},"
        f"fade=t=out:st={fade_out_start:.3f}:d={FADE_SECONDS},"
        f"format=yuv420p,"
        # Stamped on the frames themselves, not just as output flags: when the
        # source carries no colour metadata the encoder drops bare -color_*
        # options, and the upload ends up untagged and washed out.
        f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
    )


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
    )
