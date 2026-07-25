"""Render every recipe against real media. These catch filtergraph typos,
which is the failure mode that would otherwise only surface at 06:00 UTC.
"""

from pathlib import Path

import pytest

from reelforge.editor import (
    DEFAULT_RECIPES,
    HEIGHT,
    MIN_REEL_SECONDS,
    WIDTH,
    Recipe,
    VariantRenderer,
    load_recipes,
    probe,
)

ALL_RECIPES = [pytest.param(r, id=r.key) for r in DEFAULT_RECIPES]


@pytest.fixture(scope="module")
def renderer(tmp_path_factory) -> VariantRenderer:
    return VariantRenderer(tmp_path_factory.mktemp("renders"))


@pytest.mark.parametrize("recipe", ALL_RECIPES)
def test_recipe_renders_landscape_source(renderer, landscape_video: Path, recipe: Recipe):
    info = probe(landscape_video)
    output = renderer.render(landscape_video, recipe, info, f"t-{recipe.key}")

    rendered = probe(output)
    assert (rendered.width, rendered.height) == (WIDTH, HEIGHT), "must be 1080x1920"
    assert rendered.has_audio, "Reels need an audio track"
    assert rendered.duration >= MIN_REEL_SECONDS


@pytest.mark.parametrize("recipe", ALL_RECIPES)
def test_recipe_renders_silent_portrait_source(renderer, portrait_silent_video: Path, recipe: Recipe):
    """A source with no audio stream must still come out with silent audio."""
    info = probe(portrait_silent_video)
    assert not info.has_audio

    output = renderer.render(portrait_silent_video, recipe, info, f"s-{recipe.key}")
    rendered = probe(output)
    assert rendered.has_audio, "silence should have been synthesised"
    assert (rendered.width, rendered.height) == (WIDTH, HEIGHT)


@pytest.mark.parametrize("recipe", ALL_RECIPES)
def test_recipe_renders_photo_source(renderer, photo: Path, recipe: Recipe):
    """Stills become Ken Burns clips, so photos work end to end too."""
    info = probe(photo)
    output = renderer.render(photo, recipe, info, f"p-{recipe.key}")

    rendered = probe(output)
    assert (rendered.width, rendered.height) == (WIDTH, HEIGHT)
    assert rendered.duration >= MIN_REEL_SECONDS
    assert rendered.has_audio


def test_trim_never_drops_below_instagram_minimum(renderer, short_video: Path):
    """A 1.5s trim on a 4s clip would leave 2.5s, which Instagram rejects."""
    info = probe(short_video)
    recipe = Recipe("hook_trim", "trim", "trim_head", {"seconds": 1.5})

    output = renderer.render(short_video, recipe, info, "trimguard")
    assert probe(output).duration >= MIN_REEL_SECONDS


def test_speed_up_never_drops_below_instagram_minimum(renderer, short_video: Path):
    info = probe(short_video)
    recipe = Recipe("fast", "fast", "speed", {"factor": 2.0})

    output = renderer.render(short_video, recipe, info, "speedguard")
    assert probe(output).duration >= MIN_REEL_SECONDS


def test_speed_change_actually_shortens_clip(renderer, landscape_video: Path):
    info = probe(landscape_video)
    control = renderer.render(landscape_video, DEFAULT_RECIPES[0], info, "sp-control")
    fast = renderer.render(
        landscape_video, Recipe("f", "f", "speed", {"factor": 1.5}), info, "sp-fast"
    )

    assert probe(fast).duration < probe(control).duration * 0.8


def test_freeze_open_lengthens_clip(renderer, landscape_video: Path):
    info = probe(landscape_video)
    control = renderer.render(landscape_video, DEFAULT_RECIPES[0], info, "fz-control")
    frozen = renderer.render(
        landscape_video, Recipe("z", "z", "freeze_open", {"seconds": 1.0}), info, "fz-frozen"
    )

    assert probe(frozen).duration > probe(control).duration


def test_variants_are_actually_different(renderer, landscape_video: Path):
    """Two recipes that produce byte-identical output would waste a slot."""
    info = probe(landscape_video)
    control = renderer.render(landscape_video, DEFAULT_RECIPES[0], info, "d-control")
    graded = renderer.render(
        landscape_video,
        Recipe("g", "g", "grade", {"brightness": 0.06, "contrast": 1.14, "saturation": 1.28}),
        info,
        "d-graded",
    )

    assert control.read_bytes() != graded.read_bytes()


def test_unknown_recipe_kind_is_rejected(renderer, landscape_video: Path):
    info = probe(landscape_video)
    with pytest.raises(Exception, match="Unknown recipe kind"):
        renderer.render(landscape_video, Recipe("x", "x", "nonsense", {}), info, "bad")


def test_text_hook_without_font_falls_back_to_control(tmp_path, landscape_video: Path):
    """A runner with no fonts installed must not fail the whole batch."""
    fontless = VariantRenderer(tmp_path / "out", font_path="")
    fontless.font_path = None
    info = probe(landscape_video)

    output = fontless.render(
        landscape_video, Recipe("t", "t", "text_hook", {"text": "HI"}), info, "nofont"
    )
    assert probe(output).duration >= MIN_REEL_SECONDS


def test_shipped_yaml_matches_the_code(tmp_path):
    """variants.yaml is user-editable; a bad `kind` there should be caught here."""
    from reelforge.pipeline import RECIPES_PATH

    recipes = load_recipes(RECIPES_PATH, 10)
    assert len(recipes) == 10
    assert recipes[0].key == "control", "the control must stay first"

    known = {"control", "trim_head", "speed", "zoom_punch", "grade",
             "text_hook", "freeze_open", "tight_crop"}
    assert {r.kind for r in recipes} <= known


def test_load_recipes_respects_limit(tmp_path):
    from reelforge.pipeline import RECIPES_PATH

    assert len(load_recipes(RECIPES_PATH, 3)) == 3
    assert len(load_recipes(None, 10)) == 10


# -- finishing quality ---------------------------------------------------


def test_output_is_tagged_rec709(renderer, landscape_video: Path):
    """Untagged uploads are what make Reels look washed out after Instagram
    re-encodes them."""
    import json
    import subprocess

    info = probe(landscape_video)
    output = renderer.render(landscape_video, DEFAULT_RECIPES[0], info, "colour")

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
         "-print_format", "json", str(output)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]

    assert stream.get("color_space") == "bt709"
    assert stream.get("color_primaries") == "bt709"
    assert stream.get("color_transfer") == "bt709"


def test_audio_is_normalised_to_48k_stereo(renderer, landscape_video: Path):
    import json
    import subprocess

    info = probe(landscape_video)
    output = renderer.render(landscape_video, DEFAULT_RECIPES[0], info, "audio")

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams",
         "-print_format", "json", str(output)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]

    assert stream["sample_rate"] == "48000"
    assert stream["channels"] == 2


def test_grades_stay_product_safe():
    """Customers order by colour; an oversaturated render misrepresents fabric."""
    for recipe in DEFAULT_RECIPES:
        if recipe.kind != "grade":
            continue
        assert recipe.params.get("saturation", 1.0) <= 1.15, (
            f"{recipe.key} saturation is high enough to misrepresent the product"
        )
        assert recipe.params.get("contrast", 1.0) <= 1.10, f"{recipe.key} contrast too aggressive"


def test_every_variant_shares_the_same_finishing_pass(renderer, landscape_video: Path):
    """The finish must not become a confound between variants."""
    info = probe(landscape_video)
    for recipe in DEFAULT_RECIPES:
        _, graph, _ = renderer._build(recipe, info)
        assert "unsharp" in graph
        assert "fade=t=in" in graph
        assert "fade=t=out" in graph


def test_fade_out_lands_inside_a_capped_clip():
    """A clip trimmed to the Reels cap must fade at the new tail, not the old one."""
    from reelforge.editor import MAX_REEL_SECONDS, MediaInfo

    renderer = VariantRenderer(Path("/tmp"))
    info = MediaInfo(duration=300.0, width=1080, height=1920, has_audio=True)
    _, graph, post = renderer._build(DEFAULT_RECIPES[0], info)

    assert post["output_args"] == ["-t", f"{MAX_REEL_SECONDS}"]
    assert f"fade=t=out:st={MAX_REEL_SECONDS - 0.25:.3f}" in graph


def test_probe_reports_display_dimensions_for_rotated_phone_video(fixtures_dir):
    """Phones record landscape with a rotation flag; ffmpeg auto-rotates on
    decode, so probe must report what will actually be filtered."""
    import subprocess

    from reelforge.editor import _rotation_degrees

    flat = fixtures_dir / "flat.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(flat),
    ], check=True, capture_output=True)

    # Re-mux with a display matrix, which is how a phone stores orientation.
    path = fixtures_dir / "rotated.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-display_rotation", "90", "-i", str(flat),
        "-c", "copy", str(path),
    ], check=True, capture_output=True)

    info = probe(path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.is_portrait

    assert _rotation_degrees({}) == 0
    assert _rotation_degrees({"tags": {"rotate": "270"}}) == 270
    assert _rotation_degrees({"side_data_list": [{"rotation": -90}]}) == 90


def test_short_source_is_looped_to_clear_the_reel_minimum(renderer, fixtures_dir):
    """Real phone clips are routinely under Instagram's 3s floor."""
    import subprocess

    path = fixtures_dir / "tiny.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ], check=True, capture_output=True)

    info = probe(path)
    assert info.duration < MIN_REEL_SECONDS

    for recipe in DEFAULT_RECIPES:
        output = renderer.render(path, recipe, info, f"tiny-{recipe.key}")
        assert probe(output).duration >= MIN_REEL_SECONDS, (
            f"{recipe.key} came out too short for Instagram"
        )


def test_head_trim_uses_filter_not_input_seek(renderer):
    """`-ss` before `-i` combined with `-stream_loop` silently drops a loop."""
    from reelforge.editor import MediaInfo

    info = MediaInfo(duration=1.5, width=1080, height=1920, has_audio=True)
    pre, graph, _ = renderer._build(
        Recipe("hook_trim", "t", "trim_head", {"seconds": 1.5}), info
    )

    assert "-ss" not in pre
    assert "-stream_loop" in pre
    assert "trim=start=" in graph
