"""Generative video/image through fal.ai.

Higgsfield's REST API is gated behind higher tiers and its contract is not
publicly documented, so it cannot be driven unattended. fal.ai hosts the same
underlying models with a documented queue API, which is what this uses.

Kept behind `FAL_KEY`: without it the pipeline renders the deterministic
treatments only, and nothing here runs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .http import ApiError, new_session, request

log = logging.getLogger(__name__)

QUEUE_BASE = "https://queue.fal.run"
POLL_SECONDS = 10
DEFAULT_TIMEOUT = 900


class GenerativeError(RuntimeError):
    pass


@dataclass
class GenerativeResult:
    url: str = ""
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.url) and not self.error


class FalClient:
    def __init__(self, api_key: str, session=None) -> None:
        self.api_key = api_key
        self.session = session or new_session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self.api_key}", "Content-Type": "application/json"}

    def submit(self, model: str, payload: dict) -> str:
        response = request(
            self.session,
            "POST",
            f"{QUEUE_BASE}/{model}",
            label=f"fal submit {model}",
            headers=self._headers(),
            json=payload,
            timeout=120,
        )
        body = response.json()
        request_id = body.get("request_id")
        if not request_id:
            raise GenerativeError(f"fal returned no request_id: {str(body)[:300]}")
        log.info("fal job %s queued on %s", request_id, model)
        return request_id

    def wait(self, model: str, request_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = request(
                self.session,
                "GET",
                f"{QUEUE_BASE}/{model}/requests/{request_id}/status",
                label="fal status",
                headers=self._headers(),
                timeout=60,
            ).json()

            state = status.get("status")
            if state == "COMPLETED":
                return request(
                    self.session,
                    "GET",
                    f"{QUEUE_BASE}/{model}/requests/{request_id}",
                    label="fal result",
                    headers=self._headers(),
                    timeout=120,
                ).json()
            if state in {"FAILED", "CANCELLED"}:
                raise GenerativeError(f"fal job {request_id} ended as {state}")
            if time.monotonic() >= deadline:
                raise GenerativeError(f"fal job {request_id} timed out after {timeout}s")
            time.sleep(POLL_SECONDS)

    def run(self, model: str, payload: dict, *, timeout: int = DEFAULT_TIMEOUT) -> GenerativeResult:
        """Submit and wait. Failures are returned, not raised: a generative
        variant is a bonus slot and must never take the batch down with it."""
        try:
            request_id = self.submit(model, payload)
            body = self.wait(model, request_id, timeout=timeout)
        except (ApiError, GenerativeError) as exc:
            log.error("fal %s failed: %s", model, exc)
            return GenerativeResult(model=model, error=str(exc)[:400])

        url = _extract_url(body)
        if not url:
            return GenerativeResult(model=model, error=f"no media url in result: {str(body)[:300]}")
        return GenerativeResult(url=url, model=model)

    def download(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = request(
            self.session, "GET", url, label="fal download", stream=True, timeout=600
        )
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                handle.write(chunk)
        return destination


def _extract_url(body: dict) -> str:
    """fal models return media under several shapes; check the common ones."""
    for key in ("video", "image", "audio"):
        node = body.get(key)
        if isinstance(node, dict) and node.get("url"):
            return node["url"]
    for key in ("videos", "images"):
        node = body.get(key)
        if isinstance(node, list) and node and isinstance(node[0], dict):
            if node[0].get("url"):
                return node[0]["url"]
    if isinstance(body.get("url"), str):
        return body["url"]
    return ""
