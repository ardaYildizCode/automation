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
from .editor import DEFAULT_RECIPES, MediaInfo, Recipe

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

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["product", "observations", "caption", "cover_frame", "variants"],
    "properties": {
        "product": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "colour", "notes"],
            "properties": {
                "name": {"type": "string"},
                "colour": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
        "observations": {
            "type": "object",
            "additionalProperties": False,
            "required": ["exposure", "framing", "opening", "weaknesses"],
            "properties": {
                "exposure": {"type": "string"},
                "framing": {"type": "string"},
                "opening": {"type": "string"},
                "weaknesses": {"type": "string"},
            },
        },
        "caption": {"type": "string"},
        "cover_frame": {"type": "integer"},
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "kind", "rationale", "params"],
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(ALLOWED_KINDS)},
                    "rationale": {"type": "string"},
                    "params": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "seconds": {"type": "number"},
                            "factor": {"type": "number"},
                            "amount": {"type": "number"},
                            "zoom": {"type": "number"},
                            "brightness": {"type": "number"},
                            "contrast": {"type": "number"},
                            "saturation": {"type": "number"},
                            "gamma": {"type": "number"},
                            "temperature": {"type": "number"},
                            "fontsize": {"type": "number"},
                            "text": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class ArtDirection:
    recipes: list[Recipe]
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


def direct(
    client: LlmClient | None,
    source: Path,
    info: MediaInfo,
    workdir: Path,
    *,
    filename: str,
    variant_count: int,
    fallback_caption: str,
) -> ArtDirection:
    """Produce the recipe set and copy for one clip.

    Falls back to the deterministic catalogue whenever the model is
    unavailable or returns something unusable -- a batch always ships.
    """
    defaults = ArtDirection(
        recipes=list(DEFAULT_RECIPES[:variant_count]),
        caption=fallback_caption,
        source="defaults",
    )

    if client is None or not client.enabled:
        log.info("No LLM configured; using the default recipe catalogue")
        return defaults

    frames = extract_frames(source, workdir / "frames", info)
    if not frames:
        log.warning("No frames extracted; using the default recipe catalogue")
        return defaults

    orientation = "dikey" if info.is_portrait else "yatay"
    user_prompt = (
        f"Dosya adi: {filename}\n"
        f"Sure: {info.duration:.1f} saniye\n"
        f"Cozunurluk: {info.width}x{info.height} ({orientation})\n"
        f"Ses var mi: {'evet' if info.has_audio else 'hayir'}\n"
        f"Kaç kare gonderiliyor: {len(frames)} "
        f"(sirayla klibin basindan sonuna dogru, 0'dan basliyor)\n\n"
        f"Tam olarak {variant_count} varyant öner. Ilk varyant mutlaka "
        f"kind='control' olsun (hicbir degisiklik yok, karsilastirma tabani).\n"
        f"cover_frame olarak 0 ile {len(frames) - 1} arasinda, urunu en iyi "
        f"gosteren karenin indeksini ver."
    )

    try:
        raw = client.complete_json(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=RESPONSE_SCHEMA,
            images=frames,
            label="art_direction",
        )
    except (AiError, Exception) as exc:  # noqa: BLE001 - never fail the batch
        log.error("Art direction failed, falling back to defaults: %s", exc)
        return defaults

    recipes, rationales = _validate_variants(raw.get("variants"), variant_count)
    if not recipes:
        log.warning("Model proposed no usable variants; using defaults")
        return defaults

    product = raw.get("product") or {}
    caption = str(raw.get("caption") or "").strip() or fallback_caption

    direction = ArtDirection(
        recipes=recipes,
        caption=caption,
        product_name=str(product.get("name") or "").strip(),
        cover_frame=int(clamp(raw.get("cover_frame"), 0, len(frames) - 1, 0)),
        observations={k: str(v) for k, v in (raw.get("observations") or {}).items()},
        rationales=rationales,
        source="ai",
    )
    log.info(
        "Art direction: %s | %d variants | cover frame %d",
        direction.product_name or "?", len(recipes), direction.cover_frame,
    )
    return direction


def _validate_variants(proposed: object, wanted: int) -> tuple[list[Recipe], dict[str, str]]:
    """Turn model output into recipes, dropping anything unsafe."""
    if not isinstance(proposed, list):
        return [], {}

    recipes: list[Recipe] = []
    rationales: dict[str, str] = {}
    seen: set[str] = set()

    for index, entry in enumerate(proposed):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        if kind not in ALLOWED_KINDS:
            log.warning("Dropping variant with unknown kind %r", kind)
            continue

        key = _safe_key(entry.get("key"), index, seen)
        seen.add(key)

        params = _clamp_params(kind, entry.get("params"))
        if kind == "text_hook":
            text = _safe_hook(entry.get("params", {}).get("text"))
            if not text:
                log.warning("Dropping text_hook variant with unusable text")
                continue
            params["text"] = text

        recipes.append(
            Recipe(
                key=key,
                label=str(entry.get("label") or key)[:60],
                kind=kind,
                params=params,
            )
        )
        rationales[key] = str(entry.get("rationale") or "")[:300]

    if not recipes:
        return [], {}

    # A batch without an untouched baseline cannot tell "better" from "different".
    if not any(r.kind == "control" for r in recipes):
        recipes.insert(0, DEFAULT_RECIPES[0])

    # Top up from the catalogue if the model under-delivered.
    for fallback in DEFAULT_RECIPES:
        if len(recipes) >= wanted:
            break
        if fallback.key not in {r.key for r in recipes}:
            recipes.append(fallback)

    return recipes[:wanted], rationales


def _clamp_params(kind: str, params: object) -> dict:
    bounds = PARAM_BOUNDS.get(kind, {})
    supplied = params if isinstance(params, dict) else {}
    out: dict = {}
    for name, (low, high, default) in bounds.items():
        if name in supplied:
            out[name] = clamp(supplied[name], low, high, default)
    return out


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
    text = str(value or "").strip().strip('"“”')
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"[#@]", "", text)
    # drawtext cannot render emoji from the bundled fonts; strip anything
    # outside the Latin/Turkish range rather than shipping tofu boxes.
    text = "".join(ch for ch in text if ch.isprintable() and ord(ch) < 0x2000)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_HOOK_CHARS].strip().upper()
