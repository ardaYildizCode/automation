"""ffmpeg-based variant renderer.

Every variant is normalised to the same Reels-safe envelope (1080x1920, 30fps,
H.264 yuv420p, AAC stereo, faststart) so that differences in performance come
from the creative change under test and not from encoding artefacts.

The creative changes are deliberately the levers that actually move Reels
retention -- hook speed, opening frame, pacing, framing, grade -- rather than
decorative filters.
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


@dataclass
class Recipe:
    key: str
    label: str
    kind: str
    params: dict


# The ten variants. `kind` selects the builder; `params` are the tunables.
DEFAULT_RECIPES: list[Recipe] = [
    Recipe("control", "Orijinal (kontrol)", "control", {}),
    Recipe("hook_trim", "Hizli giris - ilk 1.5sn kesik", "trim_head", {"seconds": 1.5}),
    Recipe("speed_up", "%8 hizlandirilmis tempo", "speed", {"factor": 1.08}),
    Recipe("zoom_punch", "Yavas yakinlasma", "zoom_punch", {"amount": 0.16}),
    # Grades stay conservative on purpose: the product is sold by colour, and a
    # saturated render that misrepresents the fabric buys returns, not sales.
    Recipe("bright_pop", "Parlak / canli renk", "grade",
           {"brightness": 0.04, "contrast": 1.08, "saturation": 1.10, "gamma": 0.98}),
    Recipe("warm_grade", "Sicak ton", "grade",
           {"brightness": 0.02, "contrast": 1.05, "saturation": 1.05, "temperature": 0.06}),
    Recipe("clean_grade", "Temiz / soguk ton", "grade",
           {"brightness": 0.02, "contrast": 1.06, "saturation": 1.00, "temperature": -0.05}),
    Recipe("text_hook", "Ust yazi kancasi", "text_hook",
           {"text": "ISTEDIGINIZ RENK VE BEDENDE", "seconds": 3.0}),
    Recipe("freeze_open", "Donmus ilk kare", "freeze_open", {"seconds": 0.8}),
    Recipe("tight_crop", "Yakin cerceve", "tight_crop", {"zoom": 1.18}),
]


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


class VariantRenderer:
    def __init__(self, workdir: Path, font_path: str | None = None) -> None:
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.font_path = font_path or find_font()

    def render(self, source: Path, recipe: Recipe, info: MediaInfo, batch_id: str) -> Path:
        output = self.workdir / f"{batch_id}__{recipe.key}.mp4"
        pre_input, filtergraph, post = self._build(recipe, info)

        is_photo = info.duration <= 0.05
        cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        if is_photo:
            cmd += ["-loop", "1", "-framerate", str(FPS)]
        cmd += pre_input
        cmd += ["-i", str(source)]

        # Reels reliably want an audio track; synthesise silence when absent.
        if not info.has_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

        audio_in = "1:a" if not info.has_audio else "0:a"
        audio_chain = [post.pop("audio_filter", "")]
        if info.has_audio:
            # Loudness-match to Instagram's target so variants do not differ in
            # perceived volume. Pointless (and ill-defined) on synthetic silence.
            audio_chain.append(AUDIO_FINISH)

        graph = filtergraph
        audio_chain = [f for f in audio_chain if f]
        if audio_chain:
            # Brackets here are filtergraph labels...
            graph += f";[{audio_in}]{','.join(audio_chain)}[aout]"
            audio_map = "[aout]"
        else:
            # ...but a bare -map takes a stream specifier, which must not be bracketed.
            audio_map = audio_in

        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", audio_map]
        cmd += post.pop("output_args", [])
        cmd += [
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-preset", ENCODE_PRESET, "-crf", str(ENCODE_CRF),
            "-maxrate", "12M", "-bufsize", "24M",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-g", str(FPS * 2), "-keyint_min", str(FPS),
            # Instagram re-encodes on upload. Tagging Rec.709 explicitly is what
            # stops the washed-out / muddy look that untagged uploads get.
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-shortest",
            str(output),
        ]

        log.info("Rendering variant %s", recipe.key)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output.exists():
            raise RenderError(
                f"ffmpeg failed for variant {recipe.key}: {result.stderr[:600]}"
            )

        rendered = probe(output)
        if rendered.duration < MIN_REEL_SECONDS:
            raise RenderError(
                f"variant {recipe.key} is {rendered.duration:.1f}s; "
                f"Instagram rejects reels under {MIN_REEL_SECONDS:.0f}s"
            )
        return output

    # -- recipe builders ------------------------------------------------

    def _build(self, recipe: Recipe, info: MediaInfo) -> tuple[list[str], str, dict]:
        """Return (args before -i, filter_complex ending in [vout], extras)."""
        is_photo = info.duration <= 0.05
        duration = PHOTO_DURATION if is_photo else info.duration
        pre: list[str] = []
        post: dict = {}
        chain: list[str] = []

        # Phone clips are routinely shorter than Instagram's 3s reel minimum.
        # Looping the source is what a person would do by hand, and on a
        # product turn or a fabric detail it reads as a deliberate loop.
        # A recipe that cuts the head needs that much extra footage on top.
        kind, params = recipe.kind, recipe.params
        head_trim = float(params.get("seconds", 0.0)) if kind == "trim_head" else 0.0
        needed = SAFE_MIN_SECONDS + head_trim
        if not is_photo and 0 < duration < needed:
            loops = math.ceil(needed / duration) - 1
            pre += ["-stream_loop", str(loops)]
            duration *= loops + 1
            log.info(
                "Source is %.2fs; looping %dx to clear the %.2fs needed for %s",
                info.duration, loops + 1, needed, recipe.key,
            )

        # A still image becomes a Ken Burns clip before any variant styling.
        if is_photo:
            pre += ["-t", f"{PHOTO_DURATION}"]
            base = _ken_burns("0:v", "v0")
        else:
            base = _fit_to_canvas("0:v", "v0")

        chain.append(base)
        current = "v0"
        effective_duration = duration

        if kind == "trim_head":
            seconds = float(params.get("seconds", 1.5))
            # Keep clear of the Instagram minimum after trimming.
            if duration - seconds < SAFE_MIN_SECONDS:
                seconds = max(0.0, duration - SAFE_MIN_SECONDS)
            if seconds > 0:
                # Trimmed in the filter graph rather than with `-ss`: an input
                # seek combined with `-stream_loop` silently swallows a whole
                # loop iteration, so a looped short clip came out under length.
                chain.append(
                    f"[{current}]trim=start={seconds:.3f},setpts=PTS-STARTPTS[vtr]"
                )
                current = "vtr"
                post["audio_filter"] = (
                    f"atrim=start={seconds:.3f},asetpts=PTS-STARTPTS"
                )
                effective_duration = duration - seconds

        elif kind == "speed":
            factor = float(params.get("factor", 1.08))
            if effective_duration / factor < SAFE_MIN_SECONDS:
                factor = 1.0
            if factor != 1.0:
                chain.append(f"[{current}]setpts=PTS/{factor}[vsp]")
                current = "vsp"
                post["audio_filter"] = f"atempo={factor}"
                effective_duration = duration / factor

        elif kind == "zoom_punch":
            amount = float(params.get("amount", 0.16))
            span = max(effective_duration, 0.1)
            # Time-varying centre crop: cheap, smooth, and frame-accurate.
            chain.append(
                f"[{current}]crop="
                f"w='floor(iw/(1+{amount}*min(t/{span:.3f},1))/2)*2':"
                f"h='floor(ih/(1+{amount}*min(t/{span:.3f},1))/2)*2':"
                f"x='(iw-ow)/2':y='(ih-oh)/2',"
                f"scale={WIDTH}:{HEIGHT},setsar=1[vz]"
            )
            current = "vz"

        elif kind == "grade":
            chain.append(f"[{current}]{self._grade_filter(params)}[vg]")
            current = "vg"

        elif kind == "text_hook":
            filt = self._text_filter(params)
            if filt:
                chain.append(f"[{current}]{filt}[vt]")
                current = "vt"
            else:
                log.warning(
                    "text_hook falling back to a plain render "
                    "(font found: %s, drawtext available: %s)",
                    bool(self.font_path), has_drawtext(),
                )

        elif kind == "freeze_open":
            seconds = float(params.get("seconds", 0.8))
            chain.append(
                f"[{current}]tpad=start_mode=clone:start_duration={seconds}[vf]"
            )
            current = "vf"
            post["audio_filter"] = f"adelay={int(seconds * 1000)}|{int(seconds * 1000)}"
            effective_duration += seconds

        elif kind == "tight_crop":
            zoom = float(params.get("zoom", 1.18))
            chain.append(
                f"[{current}]crop=iw/{zoom}:ih/{zoom}:(iw-ow)/2:(ih-oh)/2,"
                f"scale={WIDTH}:{HEIGHT},setsar=1[vc]"
            )
            current = "vc"

        elif kind != "control":
            raise RenderError(f"Unknown recipe kind {kind!r} for variant {recipe.key}")

        # Reels cap: trim rather than let the publish call fail.
        if effective_duration > MAX_REEL_SECONDS:
            post["output_args"] = ["-t", f"{MAX_REEL_SECONDS}"]
            # The closing fade has to follow the trim, not the original tail.
            effective_duration = MAX_REEL_SECONDS

        chain.append(f"[{current}]{_finish(effective_duration)}[vout]")
        return pre, ";".join(chain), post

    @staticmethod
    def _grade_filter(params: dict) -> str:
        eq = (
            f"eq=brightness={params.get('brightness', 0.0)}"
            f":contrast={params.get('contrast', 1.0)}"
            f":saturation={params.get('saturation', 1.0)}"
            f":gamma={params.get('gamma', 1.0)}"
        )
        temperature = float(params.get("temperature", 0.0))
        if temperature:
            # Positive pushes red / pulls blue (warmer); negative does the reverse.
            eq += (
                f",colorbalance=rm={temperature:.3f}"
                f":bm={-temperature:.3f}"
                f":rh={temperature / 2:.3f}"
                f":bh={-temperature / 2:.3f}"
            )
        return eq

    def _text_filter(self, params: dict) -> str:
        if not self.font_path or not has_drawtext():
            return ""
        text = _escape_drawtext(str(params.get("text", "")).strip())
        if not text:
            return ""
        seconds = float(params.get("seconds", 3.0))
        size = int(params.get("fontsize", 58))
        # A soft drop shadow plus a hairline outline reads as designed type;
        # the translucent grey box it replaces reads as a meme caption.
        # Fading in and out avoids the text popping on and off.
        alpha = (
            f"if(lt(t,{FADE_SECONDS}),t/{FADE_SECONDS},"
            f"if(lt(t,{seconds - FADE_SECONDS:.3f}),1,"
            f"max(0,({seconds:.3f}-t)/{FADE_SECONDS})))"
        )
        common = (
            f"fontfile='{self.font_path}':text='{text}':"
            f"fontsize={size}:line_spacing=14:"
            f"x=(w-text_w)/2:enable='lt(t,{seconds})'"
        )
        return (
            f"drawtext={common}:y=h*{SAFE_TOP}+3:"
            f"fontcolor=black@0.55:alpha='{alpha}',"
            f"drawtext={common}:y=h*{SAFE_TOP}:"
            f"fontcolor=white:borderw=2:bordercolor=black@0.45:alpha='{alpha}'"
        )


def load_recipes(path: Path | None, limit: int) -> list[Recipe]:
    """Load recipes from YAML if present, else use the built-in ten."""
    recipes = DEFAULT_RECIPES
    if path and path.exists():
        import yaml  # imported lazily so the module works without PyYAML

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = raw.get("variants") or []
        if entries:
            recipes = [
                Recipe(
                    key=e["key"],
                    label=e.get("label", e["key"]),
                    kind=e.get("kind", "control"),
                    params=e.get("params") or {},
                )
                for e in entries
            ]

    keys = [r.key for r in recipes]
    if len(keys) != len(set(keys)):
        raise RenderError("Variant keys must be unique")
    return recipes[:limit]
