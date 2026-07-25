"""Shared HTTP helper: one session, sane retries, readable errors."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class ApiError(RuntimeError):
    """A non-retryable API failure, carrying the response body for triage."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "reelforge/1.0"
    return session


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    label: str,
    timeout: int = 120,
    **kwargs: Any,
) -> requests.Response:
    """Issue a request, retrying transient failures with exponential backoff."""
    last_exc: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == MAX_ATTEMPTS:
                raise ApiError(f"{label}: network failure after {attempt} attempts: {exc}") from exc
            _sleep(attempt, label, f"network error: {exc}")
            continue

        if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            _sleep(attempt, label, f"HTTP {response.status_code}", response)
            continue

        if response.status_code >= 400:
            raise ApiError(
                f"{label}: HTTP {response.status_code} -> {response.text[:800]}",
                status=response.status_code,
                body=response.text,
            )
        return response

    raise ApiError(f"{label}: exhausted retries") from last_exc


def _sleep(attempt: int, label: str, reason: str, response: "requests.Response | None" = None) -> None:
    delay = min(2 ** attempt, 60) + random.uniform(0, 1)
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                delay = max(delay, float(header))
            except ValueError:
                pass
    log.warning("%s: %s - retrying in %.1fs (attempt %d/%d)", label, reason, delay, attempt, MAX_ATTEMPTS)
    time.sleep(delay)


def poll(
    check: Callable[[], Any],
    *,
    label: str,
    timeout_seconds: int,
    interval_seconds: int = 10,
) -> Any:
    """Call `check` until it returns a truthy value or the deadline passes."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise ApiError(f"{label}: timed out after {timeout_seconds}s")
        time.sleep(interval_seconds)
