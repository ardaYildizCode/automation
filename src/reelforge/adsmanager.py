"""AI-assisted ad management.

The model reads the account and proposes actions. It never executes them.
Every proposal is checked against rules in `_vet` before anything is sent to
Meta, and a proposal that fails any rule is rejected with a recorded reason.

This split is deliberate. Ranking and spend rules are already known and
deterministic, and a language model adds risk rather than accuracy to them.
What the model is good at is reading a whole account at once and noticing what
a fixed rule set would miss. So it proposes; the rules decide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .ai import AiError, LlmClient
from .config import Config
from .http import ApiError, new_session, request
from .meta_ads import MIN_DAILY_BUDGET_TRY, AdsError

log = logging.getLogger(__name__)

# Hard limits. These are not suggestions to the model; they are enforced here.
MAX_BUDGET_CHANGE_PCT = 25
LEARNING_PERIOD_RESULTS = 50
STRUCTURAL_FAILURE_SPEND_TRY = 150
PAUSE_MIN_SPEND_TRY = 400
PAUSE_MIN_RESULTS = 30
PAUSE_COST_MULTIPLE = 1.5
MAX_ACTIONS_PER_RUN = 6

ACTIONS = {"pause", "adjust_budget", "no_action"}

SYSTEM_PROMPT = """\
You are managing a Meta ad account for a Turkish made-to-order clothing brand \
(Anne Kiz Store). Sales run through Instagram/Facebook ads into WhatsApp DMs. \
There is no working pixel and no website sales, so purchase, ROAS and \
landing-page metrics are meaningless. The only metric that tracks money is \
cost per messaging conversation started (cost_per_result).

You propose actions. You do not execute them; a rule engine vets everything \
you return and will reject proposals that break spend rules.

Judge on cost_per_result, never on CPC. A high CPC with a low cost per \
conversation is a good ad.

Do not propose pausing an ad set that has not finished learning (fewer than 50 \
results in its lifetime) unless it is structurally broken, meaning it has spent \
real money and produced zero results. "Expensive" and "broken" are different \
problems and only the broken one is urgent during learning.

Prefer few, high-conviction actions over many small ones. Returning no_action \
is a valid and often correct answer.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assessment", "actions"],
    "properties": {
        "assessment": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entity_id", "entity_type", "action", "reason"],
                "properties": {
                    "entity_id": {"type": "string"},
                    "entity_type": {"type": "string", "enum": ["adset", "ad"]},
                    "action": {"type": "string", "enum": sorted(ACTIONS)},
                    "new_daily_budget_try": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


@dataclass
class Proposal:
    entity_id: str
    entity_type: str
    action: str
    reason: str
    new_daily_budget_try: float | None = None
    accepted: bool = False
    rejection: str = ""
    applied: bool = False
    error: str = ""


@dataclass
class AdsReview:
    assessment: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    snapshot: list[dict] = field(default_factory=list)
    account_avg_cost: float = 0.0
    mode: str = "propose"

    @property
    def accepted(self) -> list[Proposal]:
        return [p for p in self.proposals if p.accepted]

    @property
    def rejected(self) -> list[Proposal]:
        return [p for p in self.proposals if not p.accepted]


class AdsManager:
    def __init__(self, config: Config, client: LlmClient | None) -> None:
        if not config.ads_requirements_met():
            raise AdsError("META_AD_ACCOUNT_ID and FACEBOOK_PAGE_ID are required.")
        self.config = config
        self.client = client
        self.session = new_session()
        self.base = config.graph_base
        self.account = f"act_{config.ad_account_id}"

    # -- reading --------------------------------------------------------

    def snapshot(self, days: int = 30) -> list[dict]:
        """Ad-set level performance, which is where budget decisions live."""
        fields = (
            "name,status,effective_status,daily_budget,optimization_goal,created_time,"
            "campaign{id,name},"
            "insights.date_preset(last_30d){spend,impressions,clicks,ctr,cpc,cpm,"
            "frequency,results,cost_per_result}"
        )
        response = request(
            self.session,
            "GET",
            f"{self.base}/{self.account}/adsets",
            label="ads snapshot",
            params={
                "access_token": self.config.ig_access_token,
                "fields": fields,
                "limit": 100,
            },
            timeout=180,
        )

        rows: list[dict] = []
        for entry in response.json().get("data", []):
            insights = ((entry.get("insights") or {}).get("data") or [{}])[0]
            spend = _number(insights.get("spend"))
            results = _first_result(insights.get("results"))
            cost = _first_result(insights.get("cost_per_result"))

            # Everything that has never spent is noise in this account; there
            # are dozens of them and they would swamp the model's context.
            if spend <= 0 and entry.get("effective_status") != "ACTIVE":
                continue

            rows.append(
                {
                    "adset_id": entry.get("id"),
                    "name": entry.get("name"),
                    "campaign": (entry.get("campaign") or {}).get("name"),
                    "campaign_id": (entry.get("campaign") or {}).get("id"),
                    "status": entry.get("effective_status"),
                    "daily_budget_try": _number(entry.get("daily_budget")) / 100,
                    "optimization_goal": entry.get("optimization_goal"),
                    "created_time": entry.get("created_time"),
                    "spend_try": round(spend, 2),
                    "results": results,
                    "cost_per_result_try": round(cost, 2) if cost else None,
                    "ctr": _number(insights.get("ctr")),
                    "cpc_try": _number(insights.get("cpc")),
                    "frequency": _number(insights.get("frequency")),
                }
            )
        return rows

    # -- deciding -------------------------------------------------------

    def review(self, *, apply: bool = False) -> AdsReview:
        rows = self.snapshot()
        review = AdsReview(snapshot=rows, mode="apply" if apply else "propose")
        if not rows:
            review.assessment = "No ad sets with spend or active status were found."
            return review

        costs = [r["cost_per_result_try"] for r in rows if r.get("cost_per_result_try")]
        review.account_avg_cost = sum(costs) / len(costs) if costs else 0.0

        if self.client is None or not self.client.enabled:
            review.assessment = "No LLM configured; reporting the account snapshot only."
            return review

        try:
            raw = self.client.complete_json(
                system=SYSTEM_PROMPT,
                user=(
                    f"Hesap ortalamasi cost_per_result: "
                    f"{review.account_avg_cost:.2f} TRY\n"
                    f"Korunan varliklar (asla dokunma): "
                    f"{', '.join(self.config.protected_entity_ids) or 'yok'}\n\n"
                    f"Ad set verileri (son 30 gun):\n"
                    f"{json.dumps(rows, ensure_ascii=False, indent=1)}"
                ),
                schema=RESPONSE_SCHEMA,
                label="ads_review",
            )
        except (AiError, ApiError) as exc:
            review.assessment = f"AI review unavailable: {exc}"
            log.error("Ads review failed: %s", exc)
            return review

        review.assessment = str(raw.get("assessment") or "").strip()
        proposals = _parse_proposals(raw.get("actions"))

        by_id = {r["adset_id"]: r for r in rows}
        for proposal in proposals[:MAX_ACTIONS_PER_RUN]:
            self._vet(proposal, by_id.get(proposal.entity_id), review.account_avg_cost)
            review.proposals.append(proposal)

        if apply:
            for proposal in review.accepted:
                self._apply(proposal)

        return review

    def _vet(self, proposal: Proposal, row: dict | None, avg_cost: float) -> None:
        """The rule engine. A proposal is rejected unless it clears every check."""
        if proposal.action == "no_action":
            proposal.rejection = "no action requested"
            return

        if proposal.entity_id in self.config.protected_entity_ids:
            proposal.rejection = "entity is on the protected list"
            return

        if row is None:
            proposal.rejection = "entity not present in the snapshot"
            return

        spend = row.get("spend_try") or 0.0
        results = row.get("results") or 0
        cost = row.get("cost_per_result_try")

        if proposal.action == "pause":
            structurally_broken = spend >= STRUCTURAL_FAILURE_SPEND_TRY and results == 0
            if structurally_broken:
                proposal.accepted = True
                return

            if results < LEARNING_PERIOD_RESULTS:
                proposal.rejection = (
                    f"still learning ({results}/{LEARNING_PERIOD_RESULTS} results) "
                    "and not structurally broken"
                )
                return
            if spend < PAUSE_MIN_SPEND_TRY:
                proposal.rejection = f"spend {spend:.0f} TRY is below the {PAUSE_MIN_SPEND_TRY} TRY floor"
                return
            if results < PAUSE_MIN_RESULTS:
                proposal.rejection = f"only {results} results, below the {PAUSE_MIN_RESULTS} minimum"
                return
            if not cost or not avg_cost or cost <= avg_cost * PAUSE_COST_MULTIPLE:
                proposal.rejection = (
                    f"cost {cost} TRY is not above {PAUSE_COST_MULTIPLE}x the "
                    f"account average ({avg_cost:.2f} TRY)"
                )
                return
            proposal.accepted = True
            return

        if proposal.action == "adjust_budget":
            current = row.get("daily_budget_try") or 0.0
            target = proposal.new_daily_budget_try
            if not target or target <= 0:
                proposal.rejection = "no usable target budget"
                return
            if target < MIN_DAILY_BUDGET_TRY:
                proposal.rejection = (
                    f"{target:.0f} TRY/day is below the {MIN_DAILY_BUDGET_TRY} TRY "
                    "learning threshold"
                )
                return
            if current > 0:
                change = abs(target - current) / current * 100
                if change > MAX_BUDGET_CHANGE_PCT:
                    proposal.rejection = (
                        f"{change:.0f}% change exceeds the {MAX_BUDGET_CHANGE_PCT}% "
                        "per-run limit"
                    )
                    return
            if results < LEARNING_PERIOD_RESULTS and target < current:
                proposal.rejection = "cutting the budget of an ad set that is still learning"
                return

            projected = self._projected_daily_total(row["adset_id"], target)
            if projected > self.config.max_account_daily_budget_try:
                proposal.rejection = (
                    f"would take account spend to {projected:.0f} TRY/day, over the "
                    f"{self.config.max_account_daily_budget_try} TRY cap"
                )
                return
            proposal.accepted = True
            return

        proposal.rejection = f"unsupported action {proposal.action!r}"

    def _projected_daily_total(self, adset_id: str, new_budget: float) -> float:
        total = new_budget
        for row in self.snapshot():
            if row["adset_id"] != adset_id and row.get("status") == "ACTIVE":
                total += row.get("daily_budget_try") or 0.0
        return total

    # -- executing ------------------------------------------------------

    def _apply(self, proposal: Proposal) -> None:
        payload: dict[str, Any] = {"access_token": self.config.ig_access_token}
        if proposal.action == "pause":
            payload["status"] = "PAUSED"
        elif proposal.action == "adjust_budget":
            payload["daily_budget"] = str(int(proposal.new_daily_budget_try * 100))
        else:
            return

        try:
            request(
                self.session,
                "POST",
                f"{self.base}/{proposal.entity_id}",
                label=f"ads {proposal.action}",
                data=payload,
            )
            proposal.applied = True
            log.info("Applied %s to %s", proposal.action, proposal.entity_id)
        except ApiError as exc:
            proposal.error = str(exc)[:300]
            log.error("Failed to apply %s to %s: %s", proposal.action, proposal.entity_id, exc)


def _parse_proposals(raw: object) -> list[Proposal]:
    if not isinstance(raw, list):
        return []
    out: list[Proposal] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip()
        if action not in ACTIONS:
            continue
        budget = entry.get("new_daily_budget_try")
        out.append(
            Proposal(
                entity_id=str(entry.get("entity_id") or "").strip(),
                entity_type=str(entry.get("entity_type") or "adset").strip(),
                action=action,
                reason=str(entry.get("reason") or "")[:400],
                new_daily_budget_try=float(budget) if isinstance(budget, (int, float)) else None,
            )
        )
    return out


def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_result(value: object) -> float:
    """Meta returns results/cost_per_result as a nested list of indicators."""
    if isinstance(value, list) and value:
        entry = value[0]
        if isinstance(entry, dict):
            values = entry.get("values")
            if isinstance(values, list) and values:
                return _number(values[0].get("value"))
    return _number(value)


def format_review(review: AdsReview) -> str:
    lines = [
        "# Reklam incelemesi",
        "",
        f"Mod: **{'uygulama' if review.mode == 'apply' else 'sadece oneri'}**",
        f"Hesap ortalamasi DM maliyeti: **{review.account_avg_cost:.2f} TRY**",
        "",
    ]
    if review.assessment:
        lines += ["## Degerlendirme", "", review.assessment, ""]

    if review.accepted:
        lines += ["## Kurallardan gecen aksiyonlar", ""]
        for p in review.accepted:
            status = "UYGULANDI" if p.applied else ("HATA: " + p.error if p.error else "onayli, bekliyor")
            detail = f" -> {p.new_daily_budget_try:.0f} TRY/gun" if p.new_daily_budget_try else ""
            lines.append(f"- `{p.entity_id}` **{p.action}**{detail} [{status}]")
            lines.append(f"  - {p.reason}")
        lines.append("")

    if review.rejected:
        lines += ["## Kurallarin reddettigi oneriler", ""]
        for p in review.rejected:
            lines.append(f"- `{p.entity_id}` {p.action}: {p.rejection}")
            if p.reason:
                lines.append(f"  - AI gerekcesi: {p.reason}")
        lines.append("")

    if not review.proposals:
        lines += ["Aksiyon onerilmedi.", ""]

    return "\n".join(lines)
