"""Environment-driven configuration.

Every secret comes from the environment (GitHub Secrets in CI, a .env file
locally). Nothing is committed. `Config.load()` fails loudly and names the
exact missing variable rather than dying deep inside an API call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Graph API v23.0 hit end-of-life on 2026-06-09; v25.0 is the current stable
# version. Override with GRAPH_VERSION when Meta ships the next one.
DEFAULT_GRAPH_VERSION = "v25.0"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            f"See README.md -> 'Secrets' for how to obtain it."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- Dropbox -------------------------------------------------------
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    dropbox_inbox: str
    dropbox_processed: str
    dropbox_renders: str

    # --- Instagram -----------------------------------------------------
    ig_user_id: str
    ig_access_token: str
    graph_version: str

    # --- Meta Ads ------------------------------------------------------
    ad_account_id: str
    facebook_page_id: str
    whatsapp_number: str
    ad_daily_budget_try: int
    ad_cover_image_hash: str

    # --- Pipeline behaviour --------------------------------------------
    variant_count: int
    trial_graduation_strategy: str
    measure_after_hours: int
    min_reach_per_variant: int
    max_batches_per_day: int
    caption_template: str
    dry_run: bool

    # --- Notifications --------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    scoring_weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        strategy = _optional("TRIAL_GRADUATION_STRATEGY", "MANUAL").upper()
        if strategy not in {"MANUAL", "SS_PERFORMANCE"}:
            raise ConfigError(
                "TRIAL_GRADUATION_STRATEGY must be MANUAL or SS_PERFORMANCE, "
                f"got {strategy!r}"
            )

        variant_count = _int("VARIANT_COUNT", 10)
        if not 1 <= variant_count <= 20:
            raise ConfigError("VARIANT_COUNT must be between 1 and 20")

        return cls(
            dropbox_app_key=_require("DROPBOX_APP_KEY"),
            dropbox_app_secret=_require("DROPBOX_APP_SECRET"),
            dropbox_refresh_token=_require("DROPBOX_REFRESH_TOKEN"),
            dropbox_inbox=_optional("DROPBOX_INBOX", "/ReelForge/Gelen"),
            dropbox_processed=_optional("DROPBOX_PROCESSED", "/ReelForge/Islenen"),
            dropbox_renders=_optional("DROPBOX_RENDERS", "/ReelForge/Varyantlar"),
            ig_user_id=_require("IG_USER_ID"),
            ig_access_token=_require("IG_ACCESS_TOKEN"),
            graph_version=_optional("GRAPH_VERSION", DEFAULT_GRAPH_VERSION),
            ad_account_id=_optional("META_AD_ACCOUNT_ID"),
            facebook_page_id=_optional("FACEBOOK_PAGE_ID"),
            whatsapp_number=_optional("WHATSAPP_NUMBER", "+905308314557"),
            ad_daily_budget_try=_int("AD_DAILY_BUDGET_TRY", 150),
            ad_cover_image_hash=_optional("AD_COVER_IMAGE_HASH"),
            variant_count=variant_count,
            trial_graduation_strategy=strategy,
            measure_after_hours=_int("MEASURE_AFTER_HOURS", 24),
            min_reach_per_variant=_int("MIN_REACH_PER_VARIANT", 300),
            max_batches_per_day=_int("MAX_BATCHES_PER_DAY", 1),
            caption_template=_optional(
                "CAPTION_TEMPLATE",
                "{product}\n\nIstediginiz renk, beden ve yasa ozel dikiliyor.\n"
                "Siparis ve fiyat icin DM veya WhatsApp: {whatsapp}",
            ),
            dry_run=_optional("DRY_RUN", "false").lower() in {"1", "true", "yes"},
            telegram_bot_token=_optional("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_optional("TELEGRAM_CHAT_ID"),
            scoring_weights={
                "retention": _float("WEIGHT_RETENTION", 0.40),
                "shares": _float("WEIGHT_SHARES", 0.25),
                "saves": _float("WEIGHT_SAVES", 0.20),
                "engagement": _float("WEIGHT_ENGAGEMENT", 0.15),
            },
        )

    @property
    def graph_base(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"

    def ads_requirements_met(self) -> bool:
        """Ad creation needs more than publishing does."""
        return bool(self.ad_account_id and self.facebook_page_id)
