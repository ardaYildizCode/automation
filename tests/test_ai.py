"""The AI layer treats model output as untrusted input. These tests pin that."""

from unittest.mock import patch

import pytest

from reelforge.adsmanager import (
    LEARNING_PERIOD_RESULTS,
    MAX_BUDGET_CHANGE_PCT,
    AdsManager,
    Proposal,
    _first_result,
    _parse_proposals,
    format_review,
)
from reelforge.ai import _parse_json, clamp
from reelforge.config import Config
from reelforge.director import (
    ALLOWED_KINDS,
    PARAM_BOUNDS,
    _clamp_params,
    _safe_hook,
    _safe_key,
    _validate_variants,
)
from reelforge.meta_ads import MIN_DAILY_BUDGET_TRY

ENV = {
    "DROPBOX_APP_KEY": "k", "DROPBOX_APP_SECRET": "s", "DROPBOX_REFRESH_TOKEN": "r",
    "IG_USER_ID": "17841415328875109", "IG_ACCESS_TOKEN": "tok",
    "META_AD_ACCOUNT_ID": "563100009538815", "FACEBOOK_PAGE_ID": "512766308577499",
}


@pytest.fixture
def config(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("AI_ADS_MODE", "MAX_ACCOUNT_DAILY_BUDGET_TRY", "PROTECTED_ENTITY_IDS"):
        monkeypatch.delenv(key, raising=False)
    return Config.load()


# -- JSON handling ------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here you go:\n{"a": 1}\nHope that helps.', {"a": 1}),
    ("", None),
    ("no json at all", None),
    ("[1,2,3]", None),  # a list is not a valid response object
])
def test_json_is_recovered_from_chatty_replies(text, expected):
    assert _parse_json(text) == expected


@pytest.mark.parametrize("value,expected", [
    (5, 5.0), ("5", 5.0), (99, 10.0), (-99, 1.0),
    (None, 3.0), ("abc", 3.0), (float("nan"), 3.0),
])
def test_clamp_coerces_or_falls_back(value, expected):
    assert clamp(value, 1.0, 10.0, 3.0) == expected


# -- art direction validation -------------------------------------------


def test_unknown_recipe_kind_is_dropped():
    recipes, _ = _validate_variants(
        [{"kind": "deepfake", "key": "x", "params": {}}], wanted=10
    )
    # Only the injected control baseline survives.
    assert all(r.kind in ALLOWED_KINDS for r in recipes)
    assert "deepfake" not in {r.kind for r in recipes}


def test_control_baseline_is_injected_when_the_model_omits_it():
    recipes, _ = _validate_variants(
        [{"kind": "speed", "key": "fast", "params": {"factor": 1.1}}], wanted=2
    )
    assert recipes[0].kind == "control"


def test_batch_is_topped_up_when_the_model_under_delivers():
    recipes, _ = _validate_variants(
        [{"kind": "control", "key": "control", "params": {}}], wanted=10
    )
    assert len(recipes) == 10
    assert len({r.key for r in recipes}) == 10, "keys must stay unique"


def test_duplicate_keys_are_made_unique():
    recipes, _ = _validate_variants([
        {"kind": "control", "key": "same", "params": {}},
        {"kind": "speed", "key": "same", "params": {"factor": 1.1}},
    ], wanted=2)
    assert len({r.key for r in recipes}) == len(recipes)


def test_saturation_beyond_the_product_safe_cap_is_clamped():
    """The model must not be able to misrepresent fabric colour."""
    params = _clamp_params("grade", {"saturation": 2.5, "contrast": 3.0})

    low, high, _ = PARAM_BOUNDS["grade"]["saturation"]
    assert params["saturation"] == high <= 1.15
    assert params["contrast"] == PARAM_BOUNDS["grade"]["contrast"][1]


def test_speed_factor_cannot_be_set_to_something_unwatchable():
    assert _clamp_params("speed", {"factor": 9.0})["factor"] == PARAM_BOUNDS["speed"]["factor"][1]


def test_unspecified_params_are_left_out_not_defaulted_in():
    """An omitted parameter should keep the renderer's own default."""
    assert _clamp_params("grade", {"saturation": 1.05}) == {"saturation": 1.05}


def test_text_hook_without_usable_text_is_dropped():
    recipes, _ = _validate_variants([
        {"kind": "control", "key": "control", "params": {}},
        {"kind": "text_hook", "key": "hook", "params": {"text": "   "}},
    ], wanted=2)
    assert "hook" not in {r.key for r in recipes}


@pytest.mark.parametrize("raw,expected", [
    ("istediğiniz renkte", "ISTEDIĞINIZ RENKTE"),
    ('  "Tırnaklı metin"  ', "TIRNAKLI METIN"),
    ("iki\nsatir", "IKI SATIR"),
    ("#hashtag @mention", "HASHTAG MENTION"),
    ("emoji var 🔥🔥", "EMOJI VAR"),
])
def test_hook_text_is_sanitised_for_drawtext(raw, expected):
    assert _safe_hook(raw) == expected


def test_hook_text_is_length_capped():
    assert len(_safe_hook("A" * 200)) <= 42


def test_keys_are_made_filesystem_and_ffmpeg_safe():
    assert _safe_key("Hızlı Giriş!!", 0, set()) == "hizli_giris"
    assert _safe_key("", 3, set()) == "variant_4"
    assert _safe_key("dup", 0, {"dup"}) == "dup_2"


def test_non_list_variant_payload_is_rejected():
    assert _validate_variants("not a list", 10) == ([], {})
    assert _validate_variants(None, 10) == ([], {})


# -- ads guard rails ----------------------------------------------------


def snapshot_row(**overrides) -> dict:
    row = {
        "adset_id": "120242204540310256",
        "name": "M/29.03",
        "status": "ACTIVE",
        "daily_budget_try": 250.0,
        "spend_try": 5000.0,
        "results": 300,
        "cost_per_result_try": 16.0,
    }
    row.update(overrides)
    return row


def vet(config, proposal: Proposal, row: dict | None, avg_cost: float = 14.05) -> Proposal:
    manager = AdsManager.__new__(AdsManager)
    manager.config = config
    manager._projected_daily_total = lambda adset_id, budget: budget + 100
    manager._vet(proposal, row, avg_cost)
    return proposal


def test_protected_entities_are_never_touched(config):
    """The purchase campaign must stay off until the pixel is fixed."""
    proposal = vet(
        config,
        Proposal("120249792730160256", "adset", "pause", "looks bad"),
        snapshot_row(adset_id="120249792730160256"),
    )
    assert not proposal.accepted
    assert "protected" in proposal.rejection


def test_pause_is_refused_during_the_learning_period(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "pause", "expensive"),
        snapshot_row(adset_id="a1", results=20, spend_try=500, cost_per_result_try=25.0),
    )
    assert not proposal.accepted
    assert "still learning" in proposal.rejection


def test_structurally_broken_adset_is_paused_even_while_learning(config):
    """Spending real money with zero results is broken, not merely expensive."""
    proposal = vet(
        config,
        Proposal("a1", "adset", "pause", "zero results"),
        snapshot_row(adset_id="a1", results=0, spend_try=400, cost_per_result_try=None),
    )
    assert proposal.accepted


def test_pause_requires_all_three_conditions(config):
    # Enough results and spend, but cost is not 1.5x the average.
    proposal = vet(
        config,
        Proposal("a1", "adset", "pause", "meh"),
        snapshot_row(adset_id="a1", results=100, spend_try=900, cost_per_result_try=15.0),
        avg_cost=14.05,
    )
    assert not proposal.accepted
    assert "not above" in proposal.rejection


def test_pause_is_accepted_when_genuinely_expensive(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "pause", "way over average"),
        snapshot_row(adset_id="a1", results=100, spend_try=900, cost_per_result_try=40.0),
        avg_cost=14.05,
    )
    assert proposal.accepted


def test_budget_swing_beyond_the_per_run_limit_is_refused(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "adjust_budget", "scale up", new_daily_budget_try=1000.0),
        snapshot_row(adset_id="a1", daily_budget_try=250.0),
    )
    assert not proposal.accepted
    assert f"{MAX_BUDGET_CHANGE_PCT}%" in proposal.rejection


def test_budget_below_the_learning_threshold_is_refused(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "adjust_budget", "cut it", new_daily_budget_try=50.0),
        snapshot_row(adset_id="a1", daily_budget_try=60.0),
    )
    assert not proposal.accepted
    assert str(MIN_DAILY_BUDGET_TRY) in proposal.rejection


def test_modest_budget_increase_is_accepted(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "adjust_budget", "working well", new_daily_budget_try=300.0),
        snapshot_row(adset_id="a1", daily_budget_try=250.0),
    )
    assert proposal.accepted


def test_account_daily_cap_is_enforced(config, monkeypatch):
    manager = AdsManager.__new__(AdsManager)
    manager.config = config
    # Everything else on the account already costs 480/day.
    manager._projected_daily_total = lambda adset_id, budget: budget + 480
    proposal = Proposal("a1", "adset", "adjust_budget", "up", new_daily_budget_try=300.0)
    manager._vet(proposal, snapshot_row(adset_id="a1", daily_budget_try=250.0), 14.05)

    assert not proposal.accepted
    assert "cap" in proposal.rejection


def test_budget_cut_during_learning_is_refused(config):
    proposal = vet(
        config,
        Proposal("a1", "adset", "adjust_budget", "too pricey", new_daily_budget_try=110.0),
        snapshot_row(adset_id="a1", daily_budget_try=130.0, results=10),
    )
    assert not proposal.accepted
    assert "still learning" in proposal.rejection


def test_proposal_for_an_entity_not_in_the_snapshot_is_refused(config):
    """Guards against the model inventing an ad set id."""
    proposal = vet(config, Proposal("hallucinated", "adset", "pause", "bad"), None)
    assert not proposal.accepted
    assert "not present" in proposal.rejection


def test_no_action_is_not_executed(config):
    proposal = vet(config, Proposal("a1", "adset", "no_action", "all fine"), snapshot_row())
    assert not proposal.accepted


def test_malformed_proposals_are_discarded():
    parsed = _parse_proposals([
        {"entity_id": "a", "action": "pause", "reason": "ok"},
        {"entity_id": "b", "action": "delete_everything", "reason": "no"},
        "not a dict",
        {"no_action_key": 1},
    ])
    assert [p.entity_id for p in parsed] == ["a"]


def test_meta_nested_result_shape_is_parsed():
    assert _first_result([{"values": [{"value": "42"}]}]) == 42.0
    assert _first_result("17.5") == 17.5
    assert _first_result(None) == 0.0


def test_review_report_shows_rejected_proposals_with_reasons(config):
    from reelforge.adsmanager import AdsReview

    review = AdsReview(assessment="Genel durum iyi.", mode="propose")
    accepted = Proposal("a1", "adset", "pause", "cok pahali")
    accepted.accepted = True
    rejected = Proposal("a2", "adset", "pause", "sezgisel")
    rejected.rejection = "still learning"
    review.proposals = [accepted, rejected]

    report = format_review(review)
    assert "a1" in report and "a2" in report
    assert "still learning" in report
    assert "sadece oneri" in report
