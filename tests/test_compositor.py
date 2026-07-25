"""Render real media through the compositor.

Filtergraph mistakes only show up when ffmpeg actually runs, and the layered
graph has far more ways to go wrong than the single-recipe one did.
"""

import json
import subprocess
from pathlib import Path

import pytest

from reelforge.compositor import Compositor
from reelforge.director import default_treatments
from reelforge.editor import HEIGHT, MIN_REEL_SECONDS, WIDTH, MediaInfo, probe
from reelforge.treatment import (
    HOOK_ANIMATIONS,
    HOOK_STYLES,
    Grade,
    Hook,
    Motion,
    Music,
    Treatment,
    available_fonts,
    control_treatment,
)


@pytest.fixture(scope="module")
def music_dir(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("music")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=30",
        "-c:a", "aac", str(path / "bed.m4a"),
    ], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def comp(tmp_path_factory, music_dir) -> Compositor:
    return Compositor(tmp_path_factory.mktemp("out"), music_dir)


def hook(**kwargs) -> Hook:
    kwargs.setdefault("text", "ISTEDIGINIZ RENKTE")
    return Hook.from_dict(kwargs, sanitiser=lambda t: str(t).upper()[:42])


DEFAULTS = default_treatments(10, ["bed.m4a"])


@pytest.mark.parametrize("treatment", [pytest.param(t, id=t.key) for t in DEFAULTS])
def test_default_catalogue_renders(comp, landscape_video: Path, treatment: Treatment):
    info = probe(landscape_video)
    output = comp.render(landscape_video, treatment, info, f"d-{treatment.key}")

    rendered = probe(output)
    assert (rendered.width, rendered.height) == (WIDTH, HEIGHT)
    assert rendered.has_audio
    assert rendered.duration >= MIN_REEL_SECONDS


@pytest.mark.parametrize("style", sorted(HOOK_STYLES))
def test_every_hook_style_renders(comp, landscape_video: Path, style: str):
    info = probe(landscape_video)
    treatment = Treatment("s", f"style {style}", hook=hook(style=style, accent_colour="burgundy"))
    assert comp.render(landscape_video, treatment, info, f"st-{style}").exists()


@pytest.mark.parametrize("animation", sorted(HOOK_ANIMATIONS))
def test_every_hook_animation_renders(comp, landscape_video: Path, animation: str):
    info = probe(landscape_video)
    treatment = Treatment("a", "anim", hook=hook(animation=animation))
    assert comp.render(landscape_video, treatment, info, f"an-{animation}").exists()


@pytest.mark.parametrize("style", sorted(HOOK_STYLES))
def test_slide_up_renders_with_every_style(comp, landscape_video: Path, style: str):
    """slide_up made `y` an expression; styles that reuse it double-quoted it
    and ffmpeg rejected the whole filter."""
    info = probe(landscape_video)
    treatment = Treatment("su", "slide", hook=hook(style=style, animation="slide_up"))
    assert comp.render(landscape_video, treatment, info, f"su-{style}").exists()


@pytest.mark.parametrize("font", available_fonts())
def test_every_bundled_font_renders_turkish(comp, landscape_video: Path, font: str):
    info = probe(landscape_video)
    treatment = Treatment("f", "font", hook=hook(text="ŞIĞÜÖÇ BEDEN", font=font))
    assert comp.render(landscape_video, treatment, info, f"fo-{font}").exists()


def test_music_bed_is_mixed_in(comp, landscape_video: Path):
    info = probe(landscape_video)
    treatment = Treatment(
        "m", "music", music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"])
    )
    output = comp.render(landscape_video, treatment, info, "music")
    assert probe(output).has_audio


def test_music_works_on_a_silent_source(comp, portrait_silent_video: Path):
    """The music input index shifts when silence has to be synthesised."""
    info = probe(portrait_silent_video)
    treatment = Treatment(
        "ms", "music silent",
        music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"]),
    )
    assert comp.render(portrait_silent_video, treatment, info, "music-silent").exists()


def test_missing_music_file_degrades_instead_of_failing(comp, landscape_video: Path):
    info = probe(landscape_video)
    treatment = Treatment("gone", "missing", music=Music(track="not-there.m4a"))
    assert comp.render(landscape_video, treatment, info, "missing-music").exists()


def test_all_layers_at_once(comp, landscape_video: Path):
    info = probe(landscape_video)
    treatment = Treatment(
        "stack", "everything",
        motion=Motion(trim_head=0.4, speed=1.06, zoom_punch=0.12, tight_crop=1.1, freeze_open=0.5),
        grade=Grade(brightness=0.03, contrast=1.05, saturation=1.08, temperature=0.05),
        hook=hook(style="underline", animation="slide_up", font="editorial"),
        music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"]),
        vignette=True,
    )
    output = comp.render(landscape_video, treatment, info, "stack")
    assert probe(output).duration >= MIN_REEL_SECONDS


def test_photo_source_renders_with_full_treatment(comp, photo: Path):
    info = probe(photo)
    treatment = Treatment(
        "p", "photo", grade=Grade(brightness=0.04),
        hook=hook(style="boxed"),
        music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"]),
    )
    output = comp.render(photo, treatment, info, "photo")
    assert probe(output).duration >= MIN_REEL_SECONDS


def test_short_source_is_looped_past_the_reel_minimum(comp, fixtures_dir):
    path = fixtures_dir / "tiny_c.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=1.4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)

    info = probe(path)
    for treatment in DEFAULTS[:4]:
        output = comp.render(path, treatment, info, f"tiny-{treatment.key}")
        assert probe(output).duration >= MIN_REEL_SECONDS, treatment.key


def test_output_is_tagged_rec709(comp, landscape_video: Path):
    info = probe(landscape_video)
    output = comp.render(landscape_video, control_treatment(), info, "colour")

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
         "-print_format", "json", str(output)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    assert stream.get("color_space") == "bt709"
    assert stream.get("color_primaries") == "bt709"


def test_control_and_treated_output_actually_differ(comp, landscape_video: Path):
    info = probe(landscape_video)
    control = comp.render(landscape_video, control_treatment(), info, "diff-c")
    treated = comp.render(
        landscape_video,
        Treatment("t", "treated", grade=Grade(brightness=0.08), hook=hook()),
        info, "diff-t",
    )
    assert control.read_bytes() != treated.read_bytes()


def test_every_treatment_shares_the_finishing_pass(comp, landscape_video: Path):
    info = probe(landscape_video)
    for treatment in DEFAULTS:
        _, graph, _, _, _ = comp._build(treatment, info, has_music=False)
        assert "unsharp" in graph
        assert "fade=t=in" in graph
        assert "setparams=color_primaries=bt709" in graph


def test_fade_out_lands_inside_a_capped_clip(comp):
    from reelforge.editor import MAX_REEL_SECONDS

    info = MediaInfo(duration=300.0, width=1080, height=1920, has_audio=True)
    _, graph, _, duration, trim_args = comp._build(control_treatment(), info, has_music=False)

    assert trim_args == ["-t", f"{MAX_REEL_SECONDS}"]
    assert duration == MAX_REEL_SECONDS
    assert f"fade=t=out:st={MAX_REEL_SECONDS - 0.25:.3f}" in graph


def test_drawbox_uses_frame_relative_coordinates(comp):
    """drawbox resolves `h` to the box height, so a bar positioned with `h`
    lands at the top of the frame instead of behind the text."""
    info = MediaInfo(duration=10.0, width=1080, height=1920, has_audio=True)
    treatment = Treatment("bar", "bar", hook=hook(style="solid_bar", position="center"))
    _, graph, _, _, _ = comp._build(treatment, info, has_music=False)

    index = graph.index("drawbox")
    bar = graph[index : graph.index(",", index)]
    assert "drawbox" in graph, "solid_bar must draw a bar"
    assert "ih*" in bar, f"drawbox must position against the input height, got {bar}"
    assert "y=h*" not in bar, "frame-height syntax in drawbox puts the bar at the top"


@pytest.mark.parametrize("with_music", [False, True])
def test_silent_source_terminates(comp, portrait_silent_video: Path, with_music: bool):
    """Synthesised silence and a looped music bed are both infinite streams.
    Without -shortest the encode never finishes, which hangs the whole batch."""
    info = probe(portrait_silent_video)
    assert not info.has_audio

    treatment = Treatment(
        "term", "terminates",
        music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"]) if with_music else None,
    )
    output = comp.render(portrait_silent_video, treatment, info, f"term-{with_music}")

    rendered = probe(output)
    # A runaway encode would produce something far longer than the source.
    assert rendered.duration < info.duration + 2.0
    assert rendered.has_audio


def test_music_does_not_outlast_the_video(comp, landscape_video: Path):
    """The bed loops indefinitely; the output must still end with the picture."""
    info = probe(landscape_video)
    treatment = Treatment(
        "len", "length",
        music=Music.from_dict({"track": "bed.m4a"}, available=["bed.m4a"]),
    )
    rendered = probe(comp.render(landscape_video, treatment, info, "music-length"))
    assert rendered.duration < info.duration + 2.0
