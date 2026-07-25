import os
import subprocess
import sys
from pathlib import Path

import pytest

# Encode speed, not encode quality, is what the render tests are checking.
os.environ.setdefault("ENCODE_PRESET", "ultrafast")
os.environ.setdefault("ENCODE_CRF", "28")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"fixture generation failed: {result.stderr[-800:]}")


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("fixtures")


@pytest.fixture(scope="session")
def landscape_video(fixtures_dir: Path) -> Path:
    """16:9 with an audio track - the awkward case that needs blurred fill."""
    path = fixtures_dir / "landscape.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ])
    return path


@pytest.fixture(scope="session")
def portrait_silent_video(fixtures_dir: Path) -> Path:
    """Already 9:16 and with no audio stream at all."""
    path = fixtures_dir / "portrait_silent.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        str(path),
    ])
    return path


@pytest.fixture(scope="session")
def short_video(fixtures_dir: Path) -> Path:
    """4s - short enough that naive trimming would drop it under Instagram's 3s floor."""
    path = fixtures_dir / "short.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ])
    return path


@pytest.fixture(scope="session")
def photo(fixtures_dir: Path) -> Path:
    path = fixtures_dir / "photo.jpg"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1600x1200", "-frames:v", "1",
        str(path),
    ])
    return path
