"""Render a Treatment: every layer composed into one ffmpeg invocation.

Layer order matters and is fixed: fit to canvas -> motion -> grade -> vignette
-> finish -> typography. Type goes last so a grade can never wash out the hook,
and the shared finish sits under it so sharpening never crawls the letterforms.
"""

from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

from .editor import (
    FADE_SECONDS,
    FPS,
    HEIGHT,
    MAX_REEL_SECONDS,
    MIN_REEL_SECONDS,
    PHOTO_DURATION,
    SAFE_MIN_SECONDS,
    UNSHARP_AMOUNT,
    WIDTH,
    MediaInfo,
    RenderError,
    _escape_drawtext,
    _fit_to_canvas,
    _ken_burns,
    has_drawtext,
    probe,
)
from .treatment import HOOK_POSITIONS, Hook, Treatment, colour, font_path

log = logging.getLogger(__name__)

ENCODE_CRF = 19
ENCODE_PRESET = "medium"
AUDIO_FINISH = "loudnorm=I=-14:TP=-1.5:LRA=11"
# How far the original audio ducks under a music bed, in dB.
DUCK_DB = -9.0


class Compositor:
    def __init__(self, workdir: Path, music_dir: Path | None = None) -> None:
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.music_dir = music_dir

    def render(
        self, source: Path, treatment: Treatment, info: MediaInfo, batch_id: str
    ) -> Path:
        output = self.workdir / f"{batch_id}__{treatment.key}.mp4"
        is_photo = info.duration <= 0.05

        music_path = self._music_path(treatment)
        pre, video_graph, audio_graph, duration, trim_args = self._build(
            treatment, info, music_path is not None
        )

        cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if is_photo:
            cmd += ["-loop", "1", "-framerate", str(FPS)]
        cmd += pre + ["-i", str(source)]

        if not info.has_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        if music_path is not None:
            music = treatment.music
            cmd += ["-ss", f"{music.start:.2f}", "-stream_loop", "-1", "-i", str(music_path)]

        graph = video_graph + (";" + audio_graph if audio_graph else "")
        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
        cmd += trim_args
        cmd += [
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-preset", ENCODE_PRESET, "-crf", str(ENCODE_CRF),
            "-maxrate", "12M", "-bufsize", "24M",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-g", str(FPS * 2), "-keyint_min", str(FPS),
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(output),
        ]

        log.info("Rendering treatment %s (%s)", treatment.key, treatment.summary())
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output.exists():
            raise RenderError(
                f"ffmpeg failed for treatment {treatment.key}: {result.stderr[:600]}"
            )

        rendered = probe(output)
        if rendered.duration < MIN_REEL_SECONDS:
            raise RenderError(
                f"treatment {treatment.key} is {rendered.duration:.1f}s; "
                f"Instagram rejects reels under {MIN_REEL_SECONDS:.0f}s"
            )
        return output

    # -- graph construction ---------------------------------------------

    def _build(
        self, treatment: Treatment, info: MediaInfo, has_music: bool
    ) -> tuple[list[str], str, str, float, list[str]]:
        is_photo = info.duration <= 0.05
        duration = PHOTO_DURATION if is_photo else info.duration
        motion = treatment.motion
        pre: list[str] = []
        chain: list[str] = []

        # Loop short clips, allowing for whatever the head trim will remove.
        if not is_photo and 0 < duration < SAFE_MIN_SECONDS + motion.trim_head:
            needed = SAFE_MIN_SECONDS + motion.trim_head
            loops = math.ceil(needed / duration) - 1
            pre += ["-stream_loop", str(loops)]
            duration *= loops + 1

        if is_photo:
            pre += ["-t", f"{PHOTO_DURATION}"]
            chain.append(_ken_burns("0:v", "v0"))
        else:
            chain.append(_fit_to_canvas("0:v", "v0"))

        current = "v0"
        audio_pre = ""

        # --- motion -----------------------------------------------------
        trim = motion.trim_head
        if trim and duration - trim < SAFE_MIN_SECONDS:
            trim = max(0.0, duration - SAFE_MIN_SECONDS)
        if trim > 0:
            chain.append(f"[{current}]trim=start={trim:.3f},setpts=PTS-STARTPTS[vtr]")
            current = "vtr"
            audio_pre = f"atrim=start={trim:.3f},asetpts=PTS-STARTPTS"
            duration -= trim

        if motion.tight_crop > 1.0:
            z = motion.tight_crop
            chain.append(
                f"[{current}]crop=iw/{z}:ih/{z}:(iw-ow)/2:(ih-oh)/2,"
                f"scale={WIDTH}:{HEIGHT},setsar=1[vcr]"
            )
            current = "vcr"

        if motion.zoom_punch > 0:
            span = max(duration, 0.1)
            chain.append(
                f"[{current}]crop="
                f"w='floor(iw/(1+{motion.zoom_punch}*min(t/{span:.3f},1))/2)*2':"
                f"h='floor(ih/(1+{motion.zoom_punch}*min(t/{span:.3f},1))/2)*2':"
                f"x='(iw-ow)/2':y='(ih-oh)/2',scale={WIDTH}:{HEIGHT},setsar=1[vzm]"
            )
            current = "vzm"

        if motion.speed != 1.0:
            factor = motion.speed
            if duration / factor < SAFE_MIN_SECONDS:
                factor = 1.0
            if factor != 1.0:
                chain.append(f"[{current}]setpts=PTS/{factor}[vsp]")
                current = "vsp"
                audio_pre = f"{audio_pre},atempo={factor}" if audio_pre else f"atempo={factor}"
                duration /= factor

        if motion.freeze_open > 0:
            hold = motion.freeze_open
            chain.append(f"[{current}]tpad=start_mode=clone:start_duration={hold}[vfz]")
            current = "vfz"
            delay = int(hold * 1000)
            pad = f"adelay={delay}|{delay}"
            audio_pre = f"{audio_pre},{pad}" if audio_pre else pad
            duration += hold

        # --- grade ------------------------------------------------------
        grade = treatment.grade
        if not grade.is_identity:
            eq = (
                f"eq=brightness={grade.brightness}:contrast={grade.contrast}"
                f":saturation={grade.saturation}:gamma={grade.gamma}"
            )
            if grade.temperature:
                t = grade.temperature
                eq += (
                    f",colorbalance=rm={t:.3f}:bm={-t:.3f}"
                    f":rh={t / 2:.3f}:bh={-t / 2:.3f}"
                )
            chain.append(f"[{current}]{eq}[vgr]")
            current = "vgr"

        if treatment.vignette:
            chain.append(f"[{current}]vignette=angle=PI/5[vvg]")
            current = "vvg"

        # --- shared finish ----------------------------------------------
        if duration > MAX_REEL_SECONDS:
            duration = MAX_REEL_SECONDS
            trim_args = ["-t", f"{MAX_REEL_SECONDS}"]
        else:
            trim_args = []

        fade_out = max(duration - FADE_SECONDS, 0.0)
        chain.append(
            f"[{current}]unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={UNSHARP_AMOUNT},"
            f"fade=t=in:st=0:d={FADE_SECONDS},fade=t=out:st={fade_out:.3f}:d={FADE_SECONDS},"
            f"format=yuv420p,"
            f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv[vfin]"
        )
        current = "vfin"

        # --- typography, last so nothing else degrades it ----------------
        hook_filter = self._hook_filter(treatment.hook)
        if hook_filter:
            chain.append(f"[{current}]{hook_filter}[vout]")
        else:
            chain.append(f"[{current}]null[vout]")

        audio_graph = self._audio_graph(info, treatment, audio_pre, has_music)
        return pre, ";".join(chain), audio_graph, duration, trim_args

    def _audio_graph(
        self, info: MediaInfo, treatment: Treatment, audio_pre: str, has_music: bool
    ) -> str:
        source_label = "0:a" if info.has_audio else "1:a"
        steps = [f for f in audio_pre.split(",") if f]

        if not has_music:
            if info.has_audio:
                steps.append(AUDIO_FINISH)
            steps.append("anull")
            return f"[{source_label}]{','.join(steps)}[aout]"

        music = treatment.music
        music_index = 2 if not info.has_audio else 1
        # Original audio ducks under the bed rather than being discarded --
        # fabric rustle and room tone are part of why the clip feels real.
        if info.has_audio and music.duck_original:
            steps.append(f"volume={DUCK_DB}dB")
        elif info.has_audio:
            steps.append(AUDIO_FINISH)
        steps.append("anull")

        return (
            f"[{source_label}]{','.join(steps)}[aorig];"
            f"[{music_index}:a]volume={music.gain_db}dB,"
            f"afade=t=in:st=0:d=0.5[amus];"
            f"[aorig][amus]amix=inputs=2:duration=shortest:dropout_transition=0,"
            f"{AUDIO_FINISH}[aout]"
        )

    def _hook_filter(self, hook: Hook | None) -> str:
        if hook is None or not has_drawtext():
            return ""
        path = font_path(hook.font)
        if path is None:
            return ""

        text = _escape_drawtext(hook.text)
        fg = colour(hook.text_colour)
        accent = colour(hook.accent_colour)
        fraction = HOOK_POSITIONS.get(hook.position, 0.14)
        # drawtext resolves `h` to the frame height; drawbox resolves it to the
        # *box* height. Mixing them puts every bar and rule at the top of the
        # frame instead of behind the text, so drawbox must use `ih`/`iw`.
        y = f"h*{fraction}"
        box_y = f"ih*{fraction}"
        enable = f"between(t,{hook.start:.2f},{hook.end:.2f})"
        alpha = self._hook_alpha(hook)
        size = hook.font_size

        if hook.animation == "slide_up":
            # Rides up into place over the first 0.35s.
            y = f"{y}+40*max(0,1-(t-{hook.start:.2f})/0.35)"
        elif hook.animation == "pop":
            size = f"'{size}*(0.86+0.14*min(1,(t-{hook.start:.2f})/0.25))'"

        base = (
            f"fontfile='{path}':text='{text}':fontsize={size}:"
            f"x=(w-text_w)/2:enable='{enable}'"
        )

        # `y` is held unquoted and wrapped exactly once at each use site;
        # quoting it earlier nests quotes and ffmpeg rejects the whole option.
        def at(offset: int = 0) -> str:
            return f"y='{y}+{offset}'" if offset else f"y='{y}'"

        if hook.style == "solid_bar":
            pad = 30
            return (
                f"drawbox=x=0:y={box_y}-{pad}:w=iw:h={hook.font_size + pad * 2}:"
                f"color={accent}@0.92:t=fill:enable='{enable}',"
                f"drawtext={base}:{at()}:fontcolor={fg}:alpha='{alpha}'"
            )
        if hook.style == "boxed":
            return (
                f"drawtext={base}:{at()}:fontcolor={fg}:alpha='{alpha}':"
                f"box=1:boxcolor={accent}@0.88:boxborderw=26"
            )
        if hook.style == "outline":
            return (
                f"drawtext={base}:{at()}:fontcolor={fg}:alpha='{alpha}':"
                f"borderw=6:bordercolor={accent}@0.95"
            )
        if hook.style == "underline":
            rule_y = f"{box_y}+{hook.font_size + 18}"
            rule_w = min(WIDTH - 120, max(240, len(hook.text) * hook.font_size // 2))
            return (
                f"drawtext={base}:{at(3)}:fontcolor=black@0.5:alpha='{alpha}',"
                f"drawtext={base}:{at()}:fontcolor={fg}:alpha='{alpha}':"
                f"borderw=2:bordercolor=black@0.45,"
                f"drawbox=x=(iw-{rule_w})/2:y={rule_y}:w={rule_w}:h=9:"
                f"color={accent}@0.95:t=fill:enable='{enable}'"
            )

        # shadow_only
        return (
            f"drawtext={base}:{at(6)}:fontcolor=black@0.60:alpha='{alpha}',"
            f"drawtext={base}:{at()}:fontcolor={fg}:alpha='{alpha}':"
            f"borderw=5:bordercolor=black@0.55"
        )

    @staticmethod
    def _hook_alpha(hook: Hook) -> str:
        if hook.animation == "none":
            return "1"
        ramp = 0.3
        start, end = hook.start, hook.end
        return (
            f"if(lt(t,{start + ramp:.2f}),max(0,(t-{start:.2f})/{ramp}),"
            f"if(lt(t,{end - ramp:.2f}),1,max(0,({end:.2f}-t)/{ramp})))"
        )

    def _music_path(self, treatment: Treatment) -> Path | None:
        if treatment.music is None or self.music_dir is None:
            return None
        path = self.music_dir / treatment.music.track
        if not path.exists():
            log.warning("Music track %s missing; rendering without it", treatment.music.track)
            return None
        return path
