"""Report delivery: GitHub Actions job summary, repo file, optional Telegram."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from .config import REPO_ROOT, Config

log = logging.getLogger(__name__)

REPORTS_DIR = REPO_ROOT / "reports"
TELEGRAM_LIMIT = 4000


def write_report(batch_id: str, markdown: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{batch_id}.md"
    path.write_text(markdown, encoding="utf-8")
    log.info("Report written to %s", path)
    return path


def to_job_summary(markdown: str) -> None:
    """Render into the Actions run page, so the report is one click from the run."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(markdown + "\n\n")


def to_telegram(config: Config, markdown: str) -> None:
    if not (config.telegram_bot_token and config.telegram_chat_id):
        return
    text = markdown if len(markdown) <= TELEGRAM_LIMIT else markdown[:TELEGRAM_LIMIT] + "\n..."
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
            data={
                "chat_id": config.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            log.warning("Telegram delivery failed: %s", response.text[:300])
    except requests.RequestException as exc:
        log.warning("Telegram delivery failed: %s", exc)


def deliver(config: Config, batch_id: str, markdown: str) -> Path:
    path = write_report(batch_id, markdown)
    to_job_summary(markdown)
    to_telegram(config, markdown)
    return path
