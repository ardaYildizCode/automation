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
    _safe_hook,
    _safe_key,
    _validate_treatments,
    default_treatments,
)
from reelforge.treatment import (
    FONTS,
    HOOK_ANIMATIONS,
    HOOK_STYLES,
    MAX_HOOK_CHARS,
    MOTION_BOUNDS,
    PALETTE,
    Grade,
    Hook,
    Motion,
    Music,
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


def test_treatment_layers_are_clamped():
    """The model must not be able to misrepresent fabric or break a render."""
    motion = Motion.from_dict({"speed": 9.0, "tight_crop": 99, "trim_head": -5})
    grade = Grade.from_dict({"saturation": 2.5, "contrast": 3.0})

    assert motion.speed == MOTION_BOUNDS["speed"][1]
    assert motion.tight_crop == MOTION_BOUNDS["tight_crop"][1]
    assert motion.trim_head == MOTION_BOUNDS["trim_head"][0]
    assert grade.saturation <= 1.15
    assert grade.contrast <= 1.10


def test_hook_falls_back_to_known_styles_fonts_and_colours():
    hook = Hook.from_dict(
        {"text": "merhaba", "font": "comic sans", "style": "explode",
         "text_colour": "#ff00ff", "accent_colour": "neon", "animation": "backflip"},
        sanitiser=_safe_hook,
    )
    assert hook.font in FONTS
    assert hook.style in HOOK_STYLES
    assert hook.text_colour in PALETTE
    assert hook.accent_colour in PALETTE
    assert hook.animation in HOOK_ANIMATIONS


def test_hook_without_text_is_dropped():
    assert Hook.from_dict({"text": "   "}, sanitiser=_safe_hook) is None
    assert Hook.from_dict(None, sanitiser=_safe_hook) is None


def test_hallucinated_music_track_becomes_no_music():
    """A filename the model invented must not crash the render."""
    assert Music.from_dict({"track": "banger.mp3"}, available=["real.m4a"]) is None
    assert Music.from_dict({"track": "real.m4a"}, available=["real.m4a"]).track == "real.m4a"


def test_music_is_skipped_when_the_library_is_empty():
    assert Music.from_dict({"track": "anything.mp3"}, available=[]) is None


def test_music_gain_is_clamped_below_unity():
    """A bed louder than the original would bury the product audio."""
    assert Music.from_dict({"track": "a.m4a", "gain_db": 40}, available=["a.m4a"]).gain_db <= 0


def test_control_baseline_is_injected_when_the_model_omits_it():
    treatments = _validate_treatments(
        [{"key": "loud", "label": "x", "motion": {"speed": 1.1}, "grade": {},
          "hook": None, "music": None, "vignette": False}],
        wanted=2, tracks=[],
    )
    assert any(t.is_control for t in treatments)


def test_batch_is_topped_up_when_the_model_under_delivers():
    treatments = _validate_treatments(
        [{"key": "control", "label": "c", "motion": {}, "grade": {},
          "hook": None, "music": None, "vignette": False}],
        wanted=10, tracks=[],
    )
    assert len(treatments) == 10
    assert len({t.key for t in treatments}) == 10


def test_duplicate_keys_are_made_unique():
    treatments = _validate_treatments([
        {"key": "same", "label": "a", "motion": {}, "grade": {}, "hook": None,
         "music": None, "vignette": False},
        {"key": "same", "label": "b", "motion": {"speed": 1.1}, "grade": {},
         "hook": None, "music": None, "vignette": False},
    ], wanted=2, tracks=[])
    assert len({t.key for t in treatments}) == len(treatments)


def test_non_list_treatment_payload_is_rejected():
    assert _validate_treatments("not a list", 10, []) == []
    assert _validate_treatments(None, 10, []) == []


def test_default_catalogue_is_genuinely_varied():
    """Ten near-identical variants would waste the whole test."""
    treatments = default_treatments(10, ["a.m4a", "b.m4a"])

    assert len(treatments) == 10
    assert treatments[0].is_control
    hooked = [t for t in treatments if t.hook]
    assert len({t.hook.style for t in hooked}) >= 4, "hook styles must differ"
    assert len({t.hook.font for t in hooked}) >= 4, "fonts must differ"
    assert len({t.hook.accent_colour for t in hooked}) >= 5, "colours must differ"
    assert len({t.hook.text for t in hooked}) == len(hooked), "hook copy must differ"
    assert any(t.music for t in treatments), "music should be used when available"


def test_default_catalogue_without_music_still_works():
    treatments = default_treatments(10, [])
    assert all(t.music is None for t in treatments)


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
    assert len(_safe_hook("A" * 200)) <= MAX_HOOK_CHARS


def test_keys_are_made_filesystem_and_ffmpeg_safe():
    assert _safe_key("Hızlı Giriş!!", 0, set()) == "hizli_giris"
    assert _safe_key("", 3, set()) == "variant_4"
    assert _safe_key("dup", 0, {"dup"}) == "dup_2"


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
