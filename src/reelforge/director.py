"""AI art direction: decide how to cut *this* clip, not a generic one.

Without this the pipeline applies the same ten recipes and the same hardcoded
hook line to every upload. The model looks at real frames and decides which
edits suit the footage, writes product-specific Turkish copy, and picks the
cover frame.

Everything it returns is treated as a proposal. Recipe kinds must exist in the
catalogue, numeric parameters are clamped to ranges that cannot produce a
broken render, and any failure falls back to the deterministic defaults.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AiError, LlmClient, clamp
from .editor import MediaInfo
from .treatment import (
    FONTS,
    HOOK_ANIMATIONS,
    HOOK_POSITIONS,
    HOOK_STYLES,
    MAX_HOOK_CHARS,
    PALETTE,
    Grade,
    Hook,
    Motion,
    Music,
    Treatment,
    control_treatment,
)

log = logging.getLogger(__name__)

FRAME_COUNT = 5
FRAME_WIDTH = 640

# What the model is allowed to touch, and how far. Outside these bounds the
# render either breaks or stops looking like the product.
PARAM_BOUNDS: dict[str, dict[str, tuple[float, float, float]]] = {
    "trim_head": {"seconds": (0.3, 4.0, 1.5)},
    "speed": {"factor": (1.0, 1.25, 1.08)},
    "zoom_punch": {"amount": (0.05, 0.35, 0.16)},
    "tight_crop": {"zoom": (1.05, 1.45, 1.18)},
    "freeze_open": {"seconds": (0.3, 1.5, 0.8)},
    "text_hook": {"seconds": (1.5, 5.0, 3.0), "fontsize": (40, 80, 58)},
    "grade": {
        "brightness": (-0.10, 0.12, 0.03),
        "contrast": (0.92, 1.10, 1.05),
        # Capped deliberately: the product is sold by colour and an
        # oversaturated render misrepresents the fabric.
        "saturation": (0.90, 1.15, 1.05),
        "gamma": (0.90, 1.10, 1.0),
        "temperature": (-0.12, 0.12, 0.0),
    },
}

ALLOWED_KINDS = set(PARAM_BOUNDS) | {"control"}
MAX_HOOK_CHARS = 42

# Keys end up in filenames and in the report table, so Turkish characters are
# transliterated rather than stripped -- "hizli_giris" beats "h_zl_giri".
TURKISH_ASCII = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
})

SYSTEM_PROMPT = """\
You are a senior short-form video editor for a Turkish mother-and-daughter \
matching clothing brand (Anne Kiz Store). Garments are made to order in the \
customer's chosen colour, size and child's age. Sales happen through Instagram \
DM and WhatsApp, never a website.

You are given frames from one piece of raw phone footage. You decide how to cut \
it into competing variants for a trial-reel test.

Rules you must follow:
- Judge the actual footage. If it opens slowly, trim harder. If the garment is \
small in frame, crop tighter. If it is underexposed, lift brightness. If it is \
already well exposed, do not "fix" it.
- Colour accuracy matters more than punch. Customers order the colour they see. \
Never propose saturation that would misrepresent fabric.
- Hook text is Turkish, upper case, at most 42 characters, and must say \
something concrete about this garment or offer. No generic slogans, no emoji, \
no hashtags, no quotes.
- Captions are Turkish, 2-4 short lines, and must end by pointing at DM/WhatsApp.
- Every variant must test a genuinely different idea. Do not propose two \
variants that differ only trivially.
"""

TREATMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["product", "observations", "caption", "cover_frame", "treatments"],
    "properties": {
        "product": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "colour", "notes"],
            "properties": {
                "name": {"type": "string"},
                "colour": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
        "observations": {
            "type": "object", "additionalProperties": False,
            "required": ["exposure", "framing", "opening", "weaknesses"],
            "properties": {
                "exposure": {"type": "string"}, "framing": {"type": "string"},
                "opening": {"type": "string"}, "weaknesses": {"type": "string"},
            },
        },
        "caption": {"type": "string"},
        "cover_frame": {"type": "integer"},
        "treatments": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["key", "label", "rationale", "motion", "grade", "hook", "music", "vignette"],
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "rationale": {"type": "string"},
                    "vignette": {"type": "boolean"},
                    "motion": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "trim_head": {"type": "number"}, "speed": {"type": "number"},
                            "zoom_punch": {"type": "number"}, "tight_crop": {"type": "number"},
                            "freeze_open": {"type": "number"},
                        },
                    },
                    "grade": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "brightness": {"type": "number"}, "contrast": {"type": "number"},
                            "saturation": {"type": "number"}, "gamma": {"type": "number"},
                            "temperature": {"type": "number"},
                        },
                    },
                    "hook": {
                        "type": ["object", "null"], "additionalProperties": False,
                        "properties": {
                            "text": {"type": "string"},
                            "font": {"type": "string", "enum": sorted(FONTS)},
                            "style": {"type": "string", "enum": sorted(HOOK_STYLES)},
                            "position": {"type": "string", "enum": sorted(HOOK_POSITIONS)},
                            "animation": {"type": "string", "enum": sorted(HOOK_ANIMATIONS)},
                            "text_colour": {"type": "string", "enum": sorted(PALETTE)},
                            "accent_colour": {"type": "string", "enum": sorted(PALETTE)},
                            "font_size": {"type": "number"},
                            "start": {"type": "number"},
                            "duration": {"type": "number"},
                        },
                    },
                    "music": {
                        "type": ["object", "null"], "additionalProperties": False,
                        "properties": {
                            "track": {"type": "string"},
                            "gain_db": {"type": "number"},
                            "start": {"type": "number"},
                            "duck_original": {"type": "boolean"},
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class ArtDirection:
    treatments: list[Treatment]
    caption: str = ""
    product_name: str = ""
    cover_frame: int = 0
    observations: dict[str, str] = field(default_factory=dict)
    rationales: dict[str, str] = field(default_factory=dict)
    source: str = "defaults"

    @property
    def ai_generated(self) -> bool:
        return self.source == "ai"


def extract_frames(source: Path, workdir: Path, info: MediaInfo, count: int = FRAME_COUNT) -> list[Path]:
    """Sample evenly across the clip so the model sees how it develops."""
    workdir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    duration = info.duration if info.duration > 0.05 else 0.0

    # A still photo has a single frame worth sampling.
    offsets = [0.0] if duration == 0.0 else [
        duration * fraction for fraction in _fractions(count)
    ]

    for index, offset in enumerate(offsets):
        target = workdir / f"frame_{index:02d}.jpg"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if duration:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += [
            "-i", str(source), "-frames:v", "1",
            "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", str(target),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and target.exists():
            frames.append(target)
        else:
            log.warning("Could not extract frame at %.2fs: %s", offset, result.stderr[:200])

    return frames


def _fractions(count: int) -> list[float]:
    if count <= 1:
        return [0.0]
    # Skip the very last frame; many clips end on a blur or a hand reaching in.
    return [i / count for i in range(count)]


def default_treatments(count: int, music_tracks: list[str]) -> list[Treatment]:
    """Fallback set used when no model is configured or it returns nothing.

    Still ten genuinely different edits -- distinct hook styles, fonts,
    colours and music beds -- rather than ten exports of the same one.
    """
    palette = [
        ("burgundy", "cream", "impact", "solid_bar", "top"),
        ("rose", "white", "modern", "boxed", "upper"),
        ("gold", "white", "condensed", "outline", "top"),
        ("navy", "white", "editorial", "underline", "top"),
        ("forest", "cream", "elegant", "shadow_only", "upper"),
        ("coral", "white", "impact", "solid_bar", "center"),
        ("lilac", "white", "condensed", "underline", "top"),
        ("black", "gold", "modern", "boxed", "top"),
        ("burgundy", "white", "editorial", "outline", "upper"),
    ]
    hooks = [
        "ISTEDIGINIZ RENK VE BEDENDE",
        "ANNE KIZ AYNI KOMBIN",
        "OLCUNUZE OZEL DIKILIYOR",
        "COCUGUNUZUN YASINA GORE",
        "EL ISCILIGI DETAYLAR",
        "SIPARIS ICIN DM",
        "YENI SEZON MODELLERI",
        "HER BEDEN MEVCUT",
        "SINIRLI SAYIDA",
    ]
    motions = [
        Motion(trim_head=1.2),
        Motion(speed=1.08),
        Motion(tight_crop=1.18),
        Motion(zoom_punch=0.16),
        Motion(freeze_open=0.8),
        Motion(trim_head=0.8, tight_crop=1.12),
        Motion(speed=1.05, zoom_punch=0.12),
        Motion(),
        Motion(trim_head=0.5, speed=1.1),
    ]
    grades = [
        Grade(),
        Grade(brightness=0.04, contrast=1.08, saturation=1.10),
        Grade(temperature=0.06, saturation=1.05),
        Grade(temperature=-0.05, contrast=1.06),
        Grade(brightness=0.03),
        Grade(contrast=1.05, saturation=1.04),
        Grade(temperature=0.04),
        Grade(brightness=0.02, contrast=1.04),
        Grade(),
    ]

    out = [control_treatment()]
    for index in range(min(count - 1, len(palette))):
        accent, text_colour, font, style, position = palette[index]
        track = music_tracks[index % len(music_tracks)] if music_tracks else ""
        out.append(
            Treatment(
                key=f"t{index + 2:02d}_{style}",
                label=f"{style} / {font} / {accent}",
                motion=motions[index],
                grade=grades[index],
                hook=Hook(
                    text=hooks[index],
                    font=font,
                    style=style,
                    position=position,
                    animation=("slide_up", "fade", "pop")[index % 3],
                    text_colour=text_colour,
                    accent_colour=accent,
                ),
                music=Music(track=track) if track else None,
                vignette=index in (4, 8),
                rationale="Varsayilan katalog",
            )
        )
    return out[:count]


def direct(
    client: LlmClient | None,
    source: Path,
    info: MediaInfo,
    workdir: Path,
    *,
    filename: str,
    variant_count: int,
    fallback_caption: str,
    music_tracks: list[str] | None = None,
) -> ArtDirection:
    """Produce the full set of treatments and copy for one clip."""
    tracks = music_tracks or []
    defaults = ArtDirection(
        treatments=default_treatments(variant_count, tracks),
        caption=fallback_caption,
        source="defaults",
    )

    if client is None or not client.enabled:
        log.info("No LLM configured; using the default treatment catalogue")
        return defaults

    frames = extract_frames(source, workdir / "frames", info)
    if not frames:
        log.warning("No frames extracted; using the default treatment catalogue")
        return defaults

    music_note = (
        "Kullanilabilir muzik dosyalari (sadece bu isimlerden birini sec, "
        f"uydurma): {', '.join(tracks)}"
        if tracks
        else "Muzik klasoru bos - her treatment icin music alanini null birak."
    )
    user_prompt = (
        f"Dosya adi: {filename}\n"
        f"Sure: {info.duration:.1f} saniye\n"
        f"Cozunurluk: {info.width}x{info.height} "
        f"({'dikey' if info.is_portrait else 'yatay'})\n"
        f"Ses var mi: {'evet' if info.has_audio else 'hayir'}\n"
        f"Gonderilen kare sayisi: {len(frames)} (bastan sona, 0'dan basliyor)\n"
        f"{music_note}\n\n"
        f"Tam olarak {variant_count} treatment uret. Ilk treatment kontrol olsun: "
        f"motion ve grade varsayilan (degisiklik yok), hook null, music null.\n"
        f"cover_frame: 0 ile {len(frames) - 1} arasinda, urunu en iyi gosteren kare."
    )

    try:
        raw = client.complete_json(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=TREATMENT_SCHEMA,
            images=frames,
            label="art_direction",
        )
    except Exception as exc:  # noqa: BLE001 - a batch must always ship
        log.error("Art direction failed, falling back to defaults: %s", exc)
        return defaults

    treatments = _validate_treatments(raw.get("treatments"), variant_count, tracks)
    if not treatments:
        log.warning("Model proposed no usable treatments; using defaults")
        return defaults

    product = raw.get("product") or {}
    caption = str(raw.get("caption") or "").strip() or fallback_caption

    direction = ArtDirection(
        treatments=treatments,
        caption=caption,
        product_name=str(product.get("name") or "").strip(),
        cover_frame=int(clamp(raw.get("cover_frame"), 0, len(frames) - 1, 0)),
        observations={k: str(v) for k, v in (raw.get("observations") or {}).items()},
        rationales={t.key: t.rationale for t in treatments},
        source="ai",
    )
    log.info(
        "Art direction: %s | %d treatments | cover frame %d",
        direction.product_name or "?", len(treatments), direction.cover_frame,
    )
    return direction


def _validate_treatments(
    proposed: object, wanted: int, tracks: list[str]
) -> list[Treatment]:
    """Turn model output into treatments, dropping anything unsafe."""
    if not isinstance(proposed, list):
        return []

    treatments: list[Treatment] = []
    seen: set[str] = set()

    for index, entry in enumerate(proposed):
        if not isinstance(entry, dict):
            continue
        key = _safe_key(entry.get("key"), index, seen)
        seen.add(key)
        treatments.append(
            Treatment(
                key=key,
                label=str(entry.get("label") or key)[:60],
                motion=Motion.from_dict(entry.get("motion")),
                grade=Grade.from_dict(entry.get("grade")),
                hook=Hook.from_dict(entry.get("hook"), sanitiser=_safe_hook),
                music=Music.from_dict(entry.get("music"), available=tracks),
                vignette=bool(entry.get("vignette")),
                rationale=str(entry.get("rationale") or "")[:300],
            )
        )

    if not treatments:
        return []

    # A batch without an untouched baseline cannot separate "better" from
    # "different", so one is inserted if the model did not provide it.
    if not any(t.is_control for t in treatments):
        treatments.insert(0, control_treatment())

    if len(treatments) < wanted:
        for extra in default_treatments(wanted, tracks):
            if len(treatments) >= wanted:
                break
            if extra.key not in {t.key for t in treatments}:
                treatments.append(extra)

    return treatments[:wanted]


def _safe_key(value: object, index: int, seen: set[str]) -> str:
    raw = str(value or "").strip().translate(TURKISH_ASCII).lower()
    key = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not key:
        key = f"variant_{index + 1}"
    while key in seen:
        key = f"{key}_2"
    return key[:32]


def _safe_hook(value: object) -> str:
    """Hook text must be short, plain and renderable by drawtext."""
    text = str(value or "").strip().strip('"\u201c\u201d')
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"[#@]", "", text)
    text = "".join(ch for ch in text if ch.isprintable() and ord(ch) < 0x2000)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_HOOK_CHARS].strip().upper()
