"""Scoring and ranking of a published trial batch.

Reels are ranked on the signals that predict whether a creative will keep
working once money is behind it: how far through people watch, and whether
they pass it on. Raw like counts are the weakest signal here and are weighted
accordingly.

All components are normalised against the best performer *within the same
batch*, so the score answers "which of these ten" rather than pretending to be
an absolute quality measure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .state import Batch, Variant

log = logging.getLogger(__name__)

# A winner this close to the runner-up is inside the noise floor.
DECISIVE_MARGIN = 0.08


@dataclass
class Component:
    key: str
    label: str
    raw: float
    normalised: float
    weight: float

    @property
    def contribution(self) -> float:
        return self.normalised * self.weight


@dataclass
class ScoredVariant:
    variant: Variant
    audience: float
    components: list[Component] = field(default_factory=list)
    score: float = 0.0
    eligible: bool = True
    reason: str = ""

    @property
    def key(self) -> str:
        return self.variant.key


@dataclass
class Ranking:
    scored: list[ScoredVariant]
    winner: ScoredVariant | None
    decisive: bool
    summary: str

    @property
    def ready(self) -> bool:
        return self.winner is not None


def _audience(insights: dict[str, float]) -> float:
    """Denominator for every rate. `views` outlives `reach`, which Meta is retiring."""
    for key in ("views", "reach", "impressions", "plays"):
        value = insights.get(key, 0.0)
        if value > 0:
            return value
    return 0.0


def _retention(insights: dict[str, float], duration: float) -> float:
    """Fraction of the reel watched on average, capped at 1.0.

    `ig_reels_avg_watch_time` is milliseconds. Falls back to total watch time
    divided by audience when the average is unavailable.
    """
    if duration <= 0:
        return 0.0

    avg_ms = insights.get("ig_reels_avg_watch_time", 0.0)
    if avg_ms <= 0:
        total_ms = insights.get("ig_reels_video_view_total_time", 0.0)
        audience = _audience(insights)
        avg_ms = total_ms / audience if total_ms > 0 and audience > 0 else 0.0

    if avg_ms <= 0:
        return 0.0
    return min(avg_ms / 1000.0 / duration, 1.0)


def score_batch(batch: Batch, weights: dict[str, float], min_audience: int) -> Ranking:
    published = batch.published_variants
    if not published:
        return Ranking([], None, False, "No variants were published in this batch.")

    scored: list[ScoredVariant] = []
    for variant in published:
        insights = variant.insights or {}
        audience = _audience(insights)
        entry = ScoredVariant(variant=variant, audience=audience)

        if audience <= 0:
            entry.eligible = False
            entry.reason = "no insight data yet"
        elif audience < min_audience:
            entry.eligible = False
            entry.reason = f"only {int(audience)} views (floor is {min_audience})"

        likes = insights.get("likes", 0.0)
        comments = insights.get("comments", 0.0)
        denominator = audience or 1.0

        entry.components = [
            Component("retention", "Izlenme orani", _retention(insights, variant.duration), 0.0,
                      weights.get("retention", 0.40)),
            Component("shares", "Paylasim / izlenme", insights.get("shares", 0.0) / denominator, 0.0,
                      weights.get("shares", 0.25)),
            Component("saves", "Kaydetme / izlenme", insights.get("saved", 0.0) / denominator, 0.0,
                      weights.get("saves", 0.20)),
            Component("engagement", "Begeni+yorum / izlenme", (likes + comments) / denominator, 0.0,
                      weights.get("engagement", 0.15)),
        ]
        scored.append(entry)

    _normalise(scored)

    for entry in scored:
        entry.score = sum(c.contribution for c in entry.components)
        entry.variant.score = round(entry.score, 4)

    scored.sort(key=lambda s: (s.eligible, s.score), reverse=True)
    for position, entry in enumerate(scored, start=1):
        entry.variant.rank = position

    eligible = [s for s in scored if s.eligible]
    if len(eligible) < 2:
        return Ranking(
            scored,
            None,
            False,
            f"Only {len(eligible)} of {len(scored)} variants cleared the {min_audience}-view floor. "
            "Waiting for more data before picking a winner.",
        )

    winner, runner_up = eligible[0], eligible[1]
    margin = (winner.score - runner_up.score) / runner_up.score if runner_up.score > 0 else 1.0
    decisive = margin >= DECISIVE_MARGIN

    summary = (
        f"{winner.key} leads with {winner.score:.3f} "
        f"({margin * 100:.0f}% ahead of {runner_up.key})."
    )
    if not decisive:
        summary += " Margin is inside the noise floor - treat as a near tie."

    return Ranking(scored, winner, decisive, summary)


def _normalise(scored: list[ScoredVariant]) -> None:
    """Scale each component against the batch best, so the leader scores 1.0.

    Divide-by-max rather than min-max: when all ten land close together,
    min-max would inflate trivial differences into apparent landslides.
    """
    if not scored:
        return
    component_count = len(scored[0].components)
    for index in range(component_count):
        peak = max(entry.components[index].raw for entry in scored)
        for entry in scored:
            component = entry.components[index]
            component.normalised = (component.raw / peak) if peak > 0 else 0.0


def format_report(batch: Batch, ranking: Ranking, *, graduation_strategy: str) -> str:
    """Markdown report: the ranking table plus the one action Arda must take."""
    lines = [
        f"# Trial batch `{batch.id}`",
        "",
        f"**Kaynak:** {batch.source_name}",
        f"**Yayinlanan varyant:** {len(batch.published_variants)}/{len(batch.variants)}",
        "",
        ranking.summary,
        "",
        "| # | Varyant | Izlenme | Izlenme orani | Paylasim | Kaydetme | Skor |",
        "|--:|---------|--------:|--------------:|---------:|---------:|-----:|",
    ]

    for entry in ranking.scored:
        components = {c.key: c for c in entry.components}
        flag = "" if entry.eligible else " *"
        lines.append(
            f"| {entry.variant.rank} | {entry.variant.label}{flag} "
            f"| {int(entry.audience):,} "
            f"| {components['retention'].raw * 100:.0f}% "
            f"| {entry.variant.insights.get('shares', 0):.0f} "
            f"| {entry.variant.insights.get('saved', 0):.0f} "
            f"| {entry.score:.3f} |"
        )

    if any(not e.eligible for e in ranking.scored):
        lines += ["", "`*` = yeterli veri yok, kazanan secimine dahil edilmedi."]

    lines.append("")
    if ranking.winner:
        winner = ranking.winner
        lines += [
            "## Yapman gereken",
            "",
            f"**Kazanan: {winner.variant.label}** (`{winner.key}`)",
        ]
        if winner.variant.permalink:
            lines.append(f"Link: {winner.variant.permalink}")
        lines.append("")
        if graduation_strategy == "MANUAL":
            lines += [
                "Instagram API'sinde deneme reel'ini normale cevirecek bir ucnokta yok.",
                "Uygulamadan tek dokunusla yap:",
                "",
                "1. Yukaridaki linki ac (veya profil > Reels > deneme sekmesi)",
                "2. Sag ustteki `...` menusunden **Herkesle paylas** / **Share with everyone**",
                "3. Sonra `promote` is akisini bu batch id ile calistir:",
                "",
                f"   `{batch.id}`",
            ]
        else:
            lines += [
                "Graduation stratejisi `SS_PERFORMANCE` - Meta iyi performans gorurse",
                "reel'i kendisi normale cevirir. Bu siralama yalnizca bilgi amacli;",
                "katilmiyorsan uygulamadan elle de mezun edebilirsin.",
            ]
    else:
        lines += ["## Yapman gereken", "", "Simdilik yok - veri esigi dolmadi, bir sonraki olcumu bekle."]

    return "\n".join(lines)
