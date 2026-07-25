import json

import pytest

from reelforge.config import Config, ConfigError
from reelforge.pipeline import build_caption, parse_source_name
from reelforge.state import Batch, BatchStatus, Store, Variant

MINIMAL_ENV = {
    "DROPBOX_APP_KEY": "k",
    "DROPBOX_APP_SECRET": "s",
    "DROPBOX_REFRESH_TOKEN": "r",
    "IG_USER_ID": "17841415328875109",
    "IG_ACCESS_TOKEN": "tok",
}


@pytest.fixture
def env(monkeypatch):
    for key in list(MINIMAL_ENV) + [
        "TRIAL_GRADUATION_STRATEGY", "VARIANT_COUNT", "META_AD_ACCOUNT_ID",
        "FACEBOOK_PAGE_ID", "DRY_RUN", "CAPTION_TEMPLATE", "GRAPH_VERSION",
        "MEASURE_AFTER_HOURS", "AD_DAILY_BUDGET_TRY",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in MINIMAL_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_config_loads_with_defaults(env):
    config = Config.load()

    assert config.variant_count == 10
    assert config.trial_graduation_strategy == "MANUAL"
    assert config.graph_base.endswith("/v25.0"), "v23 is end-of-life"
    assert not config.ads_requirements_met()


def test_missing_secret_names_the_variable(env):
    env.delenv("IG_ACCESS_TOKEN")

    with pytest.raises(ConfigError, match="IG_ACCESS_TOKEN"):
        Config.load()


def test_bad_graduation_strategy_is_rejected(env):
    env.setenv("TRIAL_GRADUATION_STRATEGY", "WHENEVER")

    with pytest.raises(ConfigError, match="MANUAL or SS_PERFORMANCE"):
        Config.load()


def test_graduation_strategy_is_case_insensitive(env):
    env.setenv("TRIAL_GRADUATION_STRATEGY", "ss_performance")
    assert Config.load().trial_graduation_strategy == "SS_PERFORMANCE"


@pytest.mark.parametrize("value", ["0", "21", "-1", "abc"])
def test_out_of_range_variant_count_is_rejected(env, value):
    env.setenv("VARIANT_COUNT", value)
    with pytest.raises(ConfigError):
        Config.load()


def test_ads_need_both_account_and_page(env):
    env.setenv("META_AD_ACCOUNT_ID", "563100009538815")
    assert not Config.load().ads_requirements_met()

    env.setenv("FACEBOOK_PAGE_ID", "512766308577499")
    assert Config.load().ads_requirements_met()


# -- filename parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("elbise_450_2-8yas.mp4", {"product": "elbise", "price": "450", "sizes": "2 8yas"}),
        ("tulum_890.mp4", {"product": "tulum", "price": "890", "sizes": ""}),
        ("IMG-20260218-WA0126.jpg", {"product": "IMG", "price": "20260218", "sizes": "WA0126"}),
        ("sadecebirisim.mp4", {"product": "sadecebirisim", "price": "", "sizes": ""}),
    ],
)
def test_parse_source_name(filename, expected):
    assert parse_source_name(filename) == expected


def test_unstructured_filename_still_produces_a_caption(env):
    caption = build_caption(Config.load(), "randomclip.mp4")

    assert "randomclip" in caption
    assert "530" in caption, "WhatsApp number should be in the caption"


def test_caption_is_identical_for_every_variant(env):
    """The edit must be the only variable in the test."""
    config = Config.load()
    assert build_caption(config, "elbise_450.mp4") == build_caption(config, "elbise_450.mp4")


# -- state --------------------------------------------------------------


def make_batch(batch_id: str = "b1", status: str = BatchStatus.PUBLISHED) -> Batch:
    return Batch(
        id=batch_id,
        source_path="/in/x.mp4",
        source_name="x.mp4",
        source_hash=f"hash-{batch_id}",
        product="x",
        created_at="2026-07-25T10:00:00+00:00",
        status=status,
        variants=[
            Variant(key="control", label="c", ig_media_id="m1", status="published"),
            Variant(key="broken", label="b", status="render_failed"),
        ],
    )


def test_state_round_trips(tmp_path):
    path = tmp_path / "batches.json"
    store = Store(path)
    store.add(make_batch())
    store.save()

    reloaded = Store(path)
    assert len(reloaded.batches) == 1
    batch = reloaded.batches[0]
    assert batch.id == "b1"
    assert len(batch.variants) == 2
    assert batch.variants[0].published
    assert not batch.variants[1].published
    assert reloaded.already_seen("hash-b1")


def test_save_is_atomic_and_valid_json(tmp_path):
    path = tmp_path / "batches.json"
    store = Store(path)
    store.add(make_batch())
    store.save()
    store.save()  # overwriting must not corrupt

    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert len(data["batches"]) == 1
    assert not list(tmp_path.glob("*.tmp")), "temp files should be cleaned up"


def test_missing_state_file_starts_empty(tmp_path):
    store = Store(tmp_path / "does-not-exist.json")
    assert store.batches == []
    assert store.seen_hashes == set()


def test_dedupe_prevents_reprocessing_the_same_upload(tmp_path):
    store = Store(tmp_path / "s.json")
    store.add(make_batch("b1"))

    assert store.already_seen("hash-b1")
    assert not store.already_seen("hash-b2")


def test_lookup_helpers(tmp_path):
    store = Store(tmp_path / "s.json")
    store.add(make_batch("b1", BatchStatus.PUBLISHED))
    store.add(make_batch("b2", BatchStatus.MEASURED))

    assert store.get("b2").status == BatchStatus.MEASURED
    assert store.get("nope") is None
    assert {b.id for b in store.by_status(BatchStatus.MEASURED)} == {"b2"}
    assert store.latest().id == "b2"


def test_winner_lookup(tmp_path):
    batch = make_batch()
    batch.winner_key = "control"

    assert batch.winner is not None
    assert batch.winner.key == "control"
    assert len(batch.published_variants) == 1


def test_unknown_fields_in_stored_state_are_ignored(tmp_path):
    """A state file written by a newer version must not crash an older one."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "seen_hashes": [],
        "batches": [{
            "id": "b1", "source_path": "/x", "source_name": "x", "source_hash": "h",
            "product": "x", "created_at": "2026-07-25T10:00:00+00:00",
            "status": "published", "variants": [], "future_field": "surprise",
        }],
    }))

    store = Store(path)
    assert store.batches[0].id == "b1"
