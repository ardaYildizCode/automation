"""Instagram client tests against a stubbed Graph API.

These pin the contract that matters: that trial_params is actually sent, and
that insight degradation works when Meta retires a metric.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from reelforge.config import Config
from reelforge.http import ApiError
from reelforge.instagram import CORE_METRICS, PREFERRED_METRICS, Instagram

MINIMAL_ENV = {
    "DROPBOX_APP_KEY": "k",
    "DROPBOX_APP_SECRET": "s",
    "DROPBOX_REFRESH_TOKEN": "r",
    "IG_USER_ID": "17841415328875109",
    "IG_ACCESS_TOKEN": "tok",
}


@pytest.fixture
def config(monkeypatch):
    for key, value in MINIMAL_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("TRIAL_GRADUATION_STRATEGY", "GRAPH_VERSION", "VARIANT_COUNT"):
        monkeypatch.delenv(key, raising=False)
    return Config.load()


def fake_response(payload: dict):
    return SimpleNamespace(
        json=lambda: payload,
        text=json.dumps(payload),
        content=json.dumps(payload).encode(),
        status_code=200,
        headers={},
    )


class Recorder:
    """Captures calls so assertions can inspect what was actually sent."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, session, method, url, *, label, **kwargs):
        self.calls.append({"method": method, "url": url, "label": label, **kwargs})
        return self.handler(method, url, kwargs)

    def find(self, fragment: str) -> dict | None:
        return next((c for c in self.calls if fragment in c["url"]), None)


# -- trial params -------------------------------------------------------


def test_trial_reel_sends_trial_params(config):
    def handler(method, url, kwargs):
        return fake_response({"id": "container-1"})

    recorder = Recorder(handler)
    with patch("reelforge.instagram.request", recorder):
        Instagram(config).create_reel_container(
            "https://dl/video.mp4", "caption", trial=True, graduation_strategy="MANUAL"
        )

    payload = recorder.calls[0]["data"]
    assert payload["media_type"] == "REELS"
    assert json.loads(payload["trial_params"]) == {"graduation_strategy": "MANUAL"}


def test_ss_performance_strategy_is_passed_through(config):
    recorder = Recorder(lambda m, u, k: fake_response({"id": "c"}))
    with patch("reelforge.instagram.request", recorder):
        Instagram(config).create_reel_container(
            "https://dl/v.mp4", "c", trial=True, graduation_strategy="SS_PERFORMANCE"
        )

    payload = json.loads(recorder.calls[0]["data"]["trial_params"])
    assert payload["graduation_strategy"] == "SS_PERFORMANCE"


def test_non_trial_reel_omits_trial_params(config):
    recorder = Recorder(lambda m, u, k: fake_response({"id": "c"}))
    with patch("reelforge.instagram.request", recorder):
        Instagram(config).create_reel_container(
            "https://dl/v.mp4", "c", trial=False, share_to_feed=True
        )

    payload = recorder.calls[0]["data"]
    assert "trial_params" not in payload
    assert payload["share_to_feed"] == "true"


# -- publish flow -------------------------------------------------------


def test_publish_reel_creates_waits_then_publishes(config):
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def handler(method, url, kwargs):
        if url.endswith("/media"):
            return fake_response({"id": "container-1"})
        if url.endswith("/media_publish"):
            return fake_response({"id": "media-9"})
        if "insights" in url:
            return fake_response({"data": []})
        if kwargs.get("params", {}).get("fields") == "permalink":
            return fake_response({"permalink": "https://instagram.com/reel/X"})
        return fake_response({"status_code": next(statuses)})

    recorder = Recorder(handler)
    with patch("reelforge.instagram.request", recorder), \
         patch("reelforge.instagram.time.sleep"), patch("reelforge.http.time.sleep"):
        result = Instagram(config).publish_reel("https://dl/v.mp4", "cap", trial=True)

    assert result.ok
    assert result.media_id == "media-9"
    assert result.permalink == "https://instagram.com/reel/X"


def test_container_error_is_captured_not_raised(config):
    """One bad variant must not abort the other nine."""
    def handler(method, url, kwargs):
        if url.endswith("/media"):
            return fake_response({"id": "container-1"})
        return fake_response({"status_code": "ERROR", "status": "transcode failed"})

    with patch("reelforge.instagram.request", Recorder(handler)), \
         patch("reelforge.instagram.time.sleep"), patch("reelforge.http.time.sleep"):
        result = Instagram(config).publish_reel("https://dl/v.mp4", "cap", trial=True)

    assert not result.ok
    assert "transcode failed" in result.error
    assert result.container_id == "container-1"


def test_container_creation_without_id_raises(config):
    with patch("reelforge.instagram.request", Recorder(lambda m, u, k: fake_response({}))):
        with pytest.raises(Exception, match="no id"):
            Instagram(config).create_reel_container("https://dl/v.mp4", "c", trial=True)


# -- eligibility --------------------------------------------------------


@pytest.mark.parametrize(
    "followers,expected",
    [(83000, True), (1000, True), (999, False), (0, False)],
)
def test_trial_availability_follows_the_1000_follower_gate(config, followers, expected):
    handler = lambda m, u, k: fake_response(
        {"username": "annekiz.store", "followers_count": followers}
    )
    with patch("reelforge.instagram.request", Recorder(handler)):
        available, detail = Instagram(config).trial_reels_available()

    assert available is expected
    if not expected:
        assert "1,000+" in detail


def test_unreadable_account_disables_trials_rather_than_crashing(config):
    def handler(method, url, kwargs):
        raise ApiError("token expired", status=400, body="{}")

    with patch("reelforge.instagram.request", Recorder(handler)):
        available, detail = Instagram(config).trial_reels_available()

    assert not available
    assert "could not read account" in detail


# -- insights degradation -----------------------------------------------


def test_insights_uses_the_full_metric_set_when_accepted(config):
    def handler(method, url, kwargs):
        return fake_response({"data": [
            {"name": "views", "values": [{"value": 1000}]},
            {"name": "shares", "values": [{"value": 12}]},
        ]})

    recorder = Recorder(handler)
    with patch("reelforge.instagram.request", recorder):
        result = Instagram(config).insights("media-1")

    assert result == {"views": 1000.0, "shares": 12.0}
    assert len(recorder.calls) == 1, "one call is enough when nothing is retired"
    assert recorder.calls[0]["params"]["metric"] == ",".join(PREFERRED_METRICS)


def test_insights_falls_back_to_core_when_a_metric_is_retired(config):
    """Meta retired several media metrics in June 2026; that must not zero the batch."""
    def handler(method, url, kwargs):
        if kwargs["params"]["metric"] == ",".join(PREFERRED_METRICS):
            raise ApiError("(#100) metric[1] must be one of...", status=400, body="")
        return fake_response({"data": [{"name": "views", "values": [{"value": 500}]}]})

    recorder = Recorder(handler)
    with patch("reelforge.instagram.request", recorder), patch("reelforge.http.time.sleep"):
        result = Instagram(config).insights("media-1")

    assert result == {"views": 500.0}
    assert recorder.calls[1]["params"]["metric"] == ",".join(CORE_METRICS)


def test_insights_falls_back_to_per_metric_calls(config):
    def handler(method, url, kwargs):
        metric = kwargs["params"]["metric"]
        if "," in metric:
            raise ApiError("batch rejected", status=400, body="")
        if metric == "views":
            return fake_response({"data": [{"name": "views", "values": [{"value": 42}]}]})
        raise ApiError("unsupported", status=400, body="")

    with patch("reelforge.instagram.request", Recorder(handler)), patch("reelforge.http.time.sleep"):
        result = Instagram(config).insights("media-1")

    assert result == {"views": 42.0}


def test_insights_returns_empty_rather_than_raising(config):
    def handler(method, url, kwargs):
        raise ApiError("all gone", status=400, body="")

    with patch("reelforge.instagram.request", Recorder(handler)), patch("reelforge.http.time.sleep"):
        assert Instagram(config).insights("media-1") == {}


def test_malformed_insight_values_are_skipped(config):
    def handler(method, url, kwargs):
        return fake_response({"data": [
            {"name": "views", "values": [{"value": 100}]},
            {"name": "broken", "values": [{"value": None}]},
            {"name": "empty", "values": []},
        ]})

    with patch("reelforge.instagram.request", Recorder(handler)):
        result = Instagram(config).insights("media-1")

    assert result["views"] == 100.0
    assert result["broken"] == 0.0
    assert "empty" not in result
