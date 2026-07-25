"""A treatment is one complete creative take on the footage.

The earlier design gave each variant a single lever -- one recipe kind, one
parameter. That produced ten renders that mostly differed by a few percent of
saturation. A treatment instead stacks every layer at once: motion, grade,
typography, music and finish. Two treatments can therefore look like genuinely
different edits rather than two exports of the same one.

Layers are independent and composable. Whatever the model proposes, the
`from_dict` constructors clamp it into a range that renders correctly and does
not misrepresent the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .ai import clamp
from .config import REPO_ROOT

FONT_DIR = REPO_ROOT / "assets" / "fonts"

# Bundled OFL faces. Typeface is the single biggest driver of whether a hook
# reads as designed or as a default, so each has a distinct personality.
FONTS: dict[str, str] = {
    "impact": "Anton-Regular.ttf",       # dense condensed, shouty
    "condensed": "BebasNeue-Regular.ttf",  # tall caps, clean
    "modern": "Montserrat.ttf",          # geometric, neutral
    "editorial": "Oswald.ttf",           # narrow, magazine
    "elegant": "PlayfairDisplay.ttf",    # serif, premium
}
DEFAULT_FONT = "condensed"

# Hook treatments. Each is a different visual language, not a colour swap.
HOOK_STYLES = {
    "solid_bar",      # full-width colour band behind the text
    "boxed",          # tight rounded box
    "outline",        # heavy outline, no fill behind
    "shadow_only",    # clean type with a soft drop shadow
    "underline",      # type with a colour rule beneath it
}
DEFAULT_HOOK_STYLE = "shadow_only"

HOOK_POSITIONS = {"top": 0.13, "upper": 0.24, "center": 0.45, "lower": 0.60}
DEFAULT_POSITION = "top"

HOOK_ANIMATIONS = {"none", "fade", "slide_up", "pop"}
DEFAULT_ANIMATION = "fade"

# Palette the model picks from. Constrained on purpose: arbitrary hex from a
# model produces muddy, low-contrast text over real footage.
PALETTE: dict[str, str] = {
    "white": "0xFFFFFF",
    "black": "0x101010",
    "cream": "0xF7F1E8",
    "rose": "0xE8899B",
    "burgundy": "0x7A2233",
    "gold": "0xC9A227",
    "navy": "0x1F3A5F",
    "forest": "0x2F5D4F",
    "coral": "0xE86A4B",
    "lilac": "0xB6A0D6",
}
DEFAULT_TEXT_COLOUR = "white"
DEFAULT_ACCENT = "burgundy"

MOTION_BOUNDS = {
    "trim_head": (0.0, 4.0, 0.0),
    "speed": (0.9, 1.25, 1.0),
    "zoom_punch": (0.0, 0.35, 0.0),
    "tight_crop": (1.0, 1.45, 1.0),
    "freeze_open": (0.0, 1.5, 0.0),
}
GRADE_BOUNDS = {
    "brightness": (-0.10, 0.12, 0.0),
    "contrast": (0.92, 1.10, 1.0),
    # The product is sold by colour; an oversaturated render misrepresents it.
    "saturation": (0.90, 1.15, 1.0),
    "gamma": (0.90, 1.10, 1.0),
    "temperature": (-0.12, 0.12, 0.0),
}
MUSIC_GAIN_BOUNDS = (-30.0, 0.0, -12.0)
MAX_HOOK_CHARS = 42


def font_path(name: str) -> Path | None:
    path = FONT_DIR / FONTS.get(name, FONTS[DEFAULT_FONT])
    return path if path.exists() else None


def available_fonts() -> list[str]:
    return [name for name in FONTS if font_path(name) is not None]


def colour(name: str, fallback: str = DEFAULT_TEXT_COLOUR) -> str:
    return PALETTE.get(name, PALETTE[fallback])


def _pick(value: object, allowed, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


@dataclass
class Motion:
    trim_head: float = 0.0
    speed: float = 1.0
    zoom_punch: float = 0.0
    tight_crop: float = 1.0
    freeze_open: float = 0.0

    @classmethod
    def from_dict(cls, data: object) -> "Motion":
        raw = data if isinstance(data, dict) else {}
        values = {}
        for name, (low, high, default) in MOTION_BOUNDS.items():
            values[name] = clamp(raw.get(name, default), low, high, default)
        return cls(**values)

    @property
    def is_identity(self) -> bool:
        return (
            self.trim_head == 0.0 and self.speed == 1.0 and self.zoom_punch == 0.0
            and self.tight_crop == 1.0 and self.freeze_open == 0.0
        )


@dataclass
class Grade:
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    temperature: float = 0.0

    @classmethod
    def from_dict(cls, data: object) -> "Grade":
        raw = data if isinstance(data, dict) else {}
        values = {}
        for name, (low, high, default) in GRADE_BOUNDS.items():
            values[name] = clamp(raw.get(name, default), low, high, default)
        return cls(**values)

    @property
    def is_identity(self) -> bool:
        return (
            self.brightness == 0.0 and self.contrast == 1.0
            and self.saturation == 1.0 and self.gamma == 1.0
            and self.temperature == 0.0
        )


@dataclass
class Hook:
    text: str = ""
    font: str = DEFAULT_FONT
    style: str = DEFAULT_HOOK_STYLE
    position: str = DEFAULT_POSITION
    animation: str = DEFAULT_ANIMATION
    text_colour: str = DEFAULT_TEXT_COLOUR
    accent_colour: str = DEFAULT_ACCENT
    font_size: int = 78
    start: float = 0.0
    duration: float = 3.0

    @classmethod
    def from_dict(cls, data: object, *, sanitiser) -> "Hook | None":
        raw = data if isinstance(data, dict) else {}
        text = sanitiser(raw.get("text"))
        if not text:
            return None
        fonts = available_fonts() or [DEFAULT_FONT]
        return cls(
            text=text,
            font=_pick(raw.get("font"), set(fonts), fonts[0]),
            style=_pick(raw.get("style"), HOOK_STYLES, DEFAULT_HOOK_STYLE),
            position=_pick(raw.get("position"), HOOK_POSITIONS, DEFAULT_POSITION),
            animation=_pick(raw.get("animation"), HOOK_ANIMATIONS, DEFAULT_ANIMATION),
            text_colour=_pick(raw.get("text_colour"), PALETTE, DEFAULT_TEXT_COLOUR),
            accent_colour=_pick(raw.get("accent_colour"), PALETTE, DEFAULT_ACCENT),
            font_size=int(clamp(raw.get("font_size"), 56, 120, 78)),
            start=clamp(raw.get("start"), 0.0, 5.0, 0.0),
            duration=clamp(raw.get("duration"), 1.0, 8.0, 3.0),
        )

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Music:
    track: str = ""
    gain_db: float = -12.0
    start: float = 0.0
    duck_original: bool = True

    @classmethod
    def from_dict(cls, data: object, *, available: list[str]) -> "Music | None":
        raw = data if isinstance(data, dict) else {}
        track = str(raw.get("track") or "").strip()
        if not available:
            return None
        if track not in available:
            # A hallucinated filename becomes "no music" rather than a crash.
            return None
        low, high, default = MUSIC_GAIN_BOUNDS
        return cls(
            track=track,
            gain_db=clamp(raw.get("gain_db"), low, high, default),
            start=clamp(raw.get("start"), 0.0, 120.0, 0.0),
            duck_original=bool(raw.get("duck_original", True)),
        )


@dataclass
class Treatment:
    key: str
    label: str
    motion: Motion = field(default_factory=Motion)
    grade: Grade = field(default_factory=Grade)
    hook: Hook | None = None
    music: Music | None = None
    vignette: bool = False
    rationale: str = ""

    @property
    def is_control(self) -> bool:
        """A control has no creative change at all beyond the shared finish."""
        return (
            self.motion.is_identity and self.grade.is_identity
            and self.hook is None and self.music is None and not self.vignette
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.motion.trim_head:
            parts.append(f"-{self.motion.trim_head:.1f}s giris")
        if self.motion.speed != 1.0:
            parts.append(f"x{self.motion.speed:.2f} hiz")
        if self.motion.zoom_punch:
            parts.append("yakinlasma")
        if self.motion.tight_crop > 1.0:
            parts.append("yakin cerceve")
        if self.motion.freeze_open:
            parts.append("donmus acilis")
        if not self.grade.is_identity:
            parts.append("renk")
        if self.hook:
            parts.append(f"{self.hook.style}/{self.hook.font}/{self.hook.text_colour}")
        if self.music:
            parts.append(f"muzik: {self.music.track}")
        return ", ".join(parts) or "degisiklik yok"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary(),
            "rationale": self.rationale,
            "hook_text": self.hook.text if self.hook else "",
            "music": self.music.track if self.music else "",
        }


def control_treatment() -> Treatment:
    return Treatment(key="control", label="Orijinal (kontrol)")
