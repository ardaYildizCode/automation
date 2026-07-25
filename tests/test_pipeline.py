"""Orchestration guard rails: the rules that stop money being spent wrongly."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from reelforge.config import Config
from reelforge.meta_ads import MIN_DAILY_BUDGET_TRY, AdsError, MetaAds, default_targeting
from reelforge.pipeline import PipelineError, _daily_quota_reached, run_graduate, run_promote
from reelforge.state import Batch, BatchStatus, Store, Variant, utcnow

ENV = {
    "DROPBOX_APP_KEY": "k",
    "DROPBOX_APP_SECRET": "s",
    "DROPBOX_REFRESH_TOKEN": "r",
    "IG_USER_ID": "17841415328875109",
    "IG_ACCESS_TOKEN": "tok",
}


@pytest.fixture
def config(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("META_AD_ACCOUNT_ID", "FACEBOOK_PAGE_ID", "MAX_BATCHES_PER_DAY",
                "AD_DAILY_BUDGET_TRY", "VARIANT_COUNT"):
        monkeypatch.delenv(key, raising=False)
    return Config.load()


@pytest.fixture
def ads_config(monkeypatch, config):
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "563100009538815")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "512766308577499")
    return Config.load()


def make_store(tmp_path, batch: Batch) -> Store:
    store = Store(tmp_path / "s.json")
    store.add(batch)
    return store


def measured_batch(status: str = BatchStatus.MEASURED) -> Batch:
    return Batch(
        id="b1", source_path="/x.mp4", source_name="x.mp4", source_hash="h",
        product="elbise", created_at=utcnow(), status=status, winner_key="control",
        variants=[
            Variant(key="control", label="Orijinal", ig_media_id="media-1",
                    status="published", permalink="https://instagram.com/reel/A"),
        ],
    )


# -- graduation ---------------------------------------------------------


def test_graduate_records_the_manual_tap(tmp_path, config):
    store = make_store(tmp_path, measured_batch())

    run_graduate(config, store, "b1")

    assert store.get("b1").status == BatchStatus.GRADUATED
    assert store.get("b1").graduated_at


def test_graduate_rejects_unknown_batch(tmp_path, config):
    store = make_store(tmp_path, measured_batch())

    with pytest.raises(PipelineError, match="Unknown batch"):
        run_graduate(config, store, "nope")


def test_graduate_refuses_before_measurement(tmp_path, config):
    batch = measured_batch(BatchStatus.PUBLISHED)
    batch.winner_key = ""

    with pytest.raises(PipelineError, match="no winner yet"):
        run_graduate(config, make_store(tmp_path, batch), "b1")


# -- promotion ----------------------------------------------------------


def test_promote_refuses_an_ungraduated_trial(tmp_path, ads_config):
    """A trial reel is invisible to followers; advertising it wastes spend."""
    store = make_store(tmp_path, measured_batch(BatchStatus.MEASURED))

    with pytest.raises(PipelineError, match="not graduated"):
        run_promote(ads_config, store, "b1")


def test_promote_is_idempotent(tmp_path, ads_config):
    batch = measured_batch(BatchStatus.GRADUATED)
    batch.ad = {"ad_id": "existing-ad"}
    store = make_store(tmp_path, batch)

    with patch("reelforge.pipeline.MetaAds") as ads:
        assert run_promote(ads_config, store, "b1") == 0
        ads.assert_not_called()


def test_promote_requires_ad_credentials(tmp_path, config):
    store = make_store(tmp_path, measured_batch(BatchStatus.GRADUATED))

    with pytest.raises(PipelineError, match="META_AD_ACCOUNT_ID"):
        run_promote(config, store, "b1")


def test_promote_refuses_when_the_winner_never_published(tmp_path, ads_config):
    batch = measured_batch(BatchStatus.GRADUATED)
    batch.variants[0].ig_media_id = ""

    with pytest.raises(PipelineError, match="no published winner"):
        run_promote(ads_config, make_store(tmp_path, batch), "b1")


# -- ad construction ----------------------------------------------------


class FakeAds:
    """Records the Graph calls the chain makes."""

    def __init__(self):
        self.posted = []

    def __call__(self, session, method, url, *, label, **kwargs):
        self.posted.append({"url": url, "data": kwargs.get("data", {})})
        name = url.rsplit("/", 1)[-1]
        return type("R", (), {
            "json": lambda self, n=name: {"id": f"{n}-id"},
            "text": "", "content": b"{}", "status_code": 200, "headers": {},
        })()

    def payload(self, edge: str) -> dict:
        return next(c["data"] for c in self.posted if c["url"].endswith(edge))


def test_ad_chain_creates_everything_paused(ads_config):
    fake = FakeAds()
    with patch("reelforge.meta_ads.request", fake):
        chain = MetaAds(ads_config).create_ad_from_ig_post(
            ig_media_id="media-1", name_prefix="RF test"
        )

    assert chain.campaign_id and chain.adset_id and chain.creative_id and chain.ad_id
    for edge in ("campaigns", "adsets", "ads"):
        assert fake.payload(edge)["status"] == "PAUSED"


def test_ad_creative_reuses_the_organic_post(ads_config):
    """Using the existing post keeps its likes and comments as social proof."""
    fake = FakeAds()
    with patch("reelforge.meta_ads.request", fake):
        MetaAds(ads_config).create_ad_from_ig_post(ig_media_id="media-1", name_prefix="RF")

    creative = fake.payload("adcreatives")
    assert creative["source_instagram_media_id"] == "media-1"
    assert creative["instagram_user_id"] == ads_config.ig_user_id


def test_budget_below_the_learning_threshold_is_raised(ads_config):
    fake = FakeAds()
    with patch("reelforge.meta_ads.request", fake):
        chain = MetaAds(ads_config).create_ad_from_ig_post(
            ig_media_id="m", name_prefix="RF", daily_budget_try=50
        )

    # Budgets are sent in minor units (kurus).
    assert fake.payload("adsets")["daily_budget"] == str(MIN_DAILY_BUDGET_TRY * 100)
    assert any("learning threshold" in w for w in chain.warnings)


def test_draft_warning_is_always_attached(ads_config):
    """API-created campaigns are deleted if the user discards drafts."""
    with patch("reelforge.meta_ads.request", FakeAds()):
        chain = MetaAds(ads_config).create_ad_from_ig_post(ig_media_id="m", name_prefix="RF")

    assert any("Review and Publish" in w for w in chain.warnings)


def test_age_bounds_are_hard_not_advisory():
    """Without this Meta spends outside the requested age range."""
    assert default_targeting()["targeting_automation"]["advantage_audience"] == 0


def test_ads_client_refuses_incomplete_credentials(config):
    with pytest.raises(AdsError, match="META_AD_ACCOUNT_ID"):
        MetaAds(config)


# -- throughput guard ---------------------------------------------------


def test_daily_quota_blocks_a_second_batch(tmp_path, config):
    store = make_store(tmp_path, measured_batch())
    assert _daily_quota_reached(store, config)


def test_yesterdays_batch_does_not_count(tmp_path, config):
    batch = measured_batch()
    batch.created_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    store = make_store(tmp_path, batch)

    assert not _daily_quota_reached(store, config)
