"""Instagram Graph API client for trial-reel publishing and insights.

Publishing a reel is a three-step dance: create a container pointing at a
publicly reachable video URL, wait for Instagram to finish transcoding it,
then publish the container. Trial reels add `trial_params` at container
creation time -- there is no way to convert a normal reel into a trial one
afterwards, and no API to graduate a trial reel back out (see README).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .config import Config
from .http import ApiError, new_session, poll, request

log = logging.getLogger(__name__)

CONTAINER_TIMEOUT_SECONDS = 600
CONTAINER_POLL_SECONDS = 10
# Instagram throttles rapid publishes; a short gap avoids spurious failures.
PUBLISH_GAP_SECONDS = 8

# Requested best-effort. Meta retired several media metrics in June 2026, so
# anything unavailable is dropped rather than failing the whole run.
PREFERRED_METRICS = [
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
]
CORE_METRICS = ["views", "likes", "comments", "shares", "saved"]


class InstagramError(RuntimeError):
    pass


@dataclass
class PublishResult:
    container_id: str
    media_id: str = ""
    permalink: str = ""
    error: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.media_id) and not self.error


class Instagram:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = new_session()
        self.base = config.graph_base
        self.user_id = config.ig_user_id

    def _params(self, **extra) -> dict:
        return {"access_token": self.config.ig_access_token, **extra}

    # -- account --------------------------------------------------------

    def account(self) -> dict:
        response = request(
            self.session,
            "GET",
            f"{self.base}/{self.user_id}",
            label="ig account",
            params=self._params(fields="id,username,followers_count,media_count"),
        )
        return response.json()

    def trial_reels_available(self) -> tuple[bool, str]:
        """Meta gates trial reels behind a 1,000-follower minimum."""
        try:
            info = self.account()
        except ApiError as exc:
            return False, f"could not read account: {exc}"
        followers = int(info.get("followers_count") or 0)
        if followers < 1000:
            return False, f"{info.get('username')} has {followers} followers; trial reels need 1,000+"
        return True, f"{info.get('username')} has {followers:,} followers"

    # -- publishing -----------------------------------------------------

    def create_reel_container(
        self,
        video_url: str,
        caption: str,
        *,
        trial: bool,
        graduation_strategy: str = "MANUAL",
        cover_url: str = "",
        share_to_feed: bool | None = None,
    ) -> str:
        payload = self._params(
            media_type="REELS",
            video_url=video_url,
            caption=caption,
        )
        if cover_url:
            payload["cover_url"] = cover_url

        if trial:
            # A trial reel is shown only to non-followers. MANUAL keeps the
            # graduation decision with us; SS_PERFORMANCE hands it to Meta.
            payload["trial_params"] = json.dumps(
                {"graduation_strategy": graduation_strategy}
            )
        elif share_to_feed is not None:
            payload["share_to_feed"] = "true" if share_to_feed else "false"

        response = request(
            self.session,
            "POST",
            f"{self.base}/{self.user_id}/media",
            label="ig create container",
            data=payload,
        )
        container_id = response.json().get("id")
        if not container_id:
            raise InstagramError(f"container creation returned no id: {response.text[:400]}")
        log.info("Created container %s (trial=%s)", container_id, trial)
        return container_id

    def wait_for_container(self, container_id: str) -> None:
        def check() -> bool:
            response = request(
                self.session,
                "GET",
                f"{self.base}/{container_id}",
                label="ig container status",
                params=self._params(fields="status_code,status"),
            )
            data = response.json()
            code = data.get("status_code", "")
            if code == "FINISHED":
                return True
            if code in {"ERROR", "EXPIRED"}:
                raise InstagramError(
                    f"container {container_id} failed: {data.get('status') or code}"
                )
            log.info("Container %s status=%s", container_id, code or "IN_PROGRESS")
            return False

        poll(
            check,
            label=f"ig container {container_id}",
            timeout_seconds=CONTAINER_TIMEOUT_SECONDS,
            interval_seconds=CONTAINER_POLL_SECONDS,
        )

    def publish(self, container_id: str) -> str:
        response = request(
            self.session,
            "POST",
            f"{self.base}/{self.user_id}/media_publish",
            label="ig publish",
            data=self._params(creation_id=container_id),
        )
        media_id = response.json().get("id")
        if not media_id:
            raise InstagramError(f"publish returned no media id: {response.text[:400]}")
        log.info("Published media %s", media_id)
        return media_id

    def publish_reel(
        self,
        video_url: str,
        caption: str,
        *,
        trial: bool,
        graduation_strategy: str = "MANUAL",
    ) -> PublishResult:
        """Full create -> wait -> publish cycle for one reel."""
        container_id = self.create_reel_container(
            video_url, caption, trial=trial, graduation_strategy=graduation_strategy
        )
        result = PublishResult(container_id=container_id)
        try:
            self.wait_for_container(container_id)
            result.media_id = self.publish(container_id)
            result.permalink = self.permalink(result.media_id)
        except (ApiError, InstagramError) as exc:
            result.error = str(exc)[:500]
            log.error("Publish failed for container %s: %s", container_id, exc)
        time.sleep(PUBLISH_GAP_SECONDS)
        return result

    def permalink(self, media_id: str) -> str:
        try:
            response = request(
                self.session,
                "GET",
                f"{self.base}/{media_id}",
                label="ig permalink",
                params=self._params(fields="permalink"),
            )
            return response.json().get("permalink", "")
        except ApiError as exc:
            log.warning("Could not read permalink for %s: %s", media_id, exc)
            return ""

    # -- insights -------------------------------------------------------

    def insights(self, media_id: str) -> dict[str, float]:
        """Fetch metrics, degrading gracefully as Meta retires them.

        Tries the full wish-list first (one call), falls back to a core set,
        and only then pays for per-metric calls.
        """
        for metrics in (PREFERRED_METRICS, CORE_METRICS):
            try:
                return self._insights_call(media_id, metrics)
            except ApiError as exc:
                log.warning(
                    "Insights for %s rejected %d-metric request: %s",
                    media_id, len(metrics), str(exc)[:200],
                )

        collected: dict[str, float] = {}
        for metric in PREFERRED_METRICS:
            try:
                collected.update(self._insights_call(media_id, [metric]))
            except ApiError:
                continue
        if not collected:
            log.error("No insights available for %s", media_id)
        return collected

    def _insights_call(self, media_id: str, metrics: list[str]) -> dict[str, float]:
        response = request(
            self.session,
            "GET",
            f"{self.base}/{media_id}/insights",
            label="ig insights",
            params=self._params(metric=",".join(metrics)),
        )
        out: dict[str, float] = {}
        for entry in response.json().get("data", []):
            name = entry.get("name")
            values = entry.get("values") or []
            if not name or not values:
                continue
            try:
                out[name] = float(values[0].get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
        return out
