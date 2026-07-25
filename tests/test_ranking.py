from reelforge.ranking import DECISIVE_MARGIN, format_report, score_batch
from reelforge.state import Batch, BatchStatus, Variant

WEIGHTS = {"retention": 0.40, "shares": 0.25, "saves": 0.20, "engagement": 0.15}


def make_variant(key: str, *, views: float, avg_watch_ms: float, duration: float = 10.0,
                 shares: float = 0, saved: float = 0, likes: float = 0,
                 comments: float = 0) -> Variant:
    return Variant(
        key=key,
        label=key,
        duration=duration,
        ig_media_id=f"media-{key}",
        status="published",
        insights={
            "views": views,
            "ig_reels_avg_watch_time": avg_watch_ms,
            "shares": shares,
            "saved": saved,
            "likes": likes,
            "comments": comments,
        },
    )


def make_batch(variants: list[Variant]) -> Batch:
    return Batch(
        id="test-batch",
        source_path="/x.mp4",
        source_name="x.mp4",
        source_hash="h",
        product="x",
        created_at="2026-07-25T10:00:00+00:00",
        status=BatchStatus.PUBLISHED,
        variants=variants,
    )


def test_higher_retention_wins_all_else_equal():
    batch = make_batch([
        make_variant("weak", views=1000, avg_watch_ms=2000),
        make_variant("strong", views=1000, avg_watch_ms=8000),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    assert ranking.winner is not None
    assert ranking.winner.key == "strong"
    assert ranking.scored[0].variant.rank == 1


def test_variant_below_view_floor_cannot_win():
    """A lucky 12-view reel must not beat a 5,000-view one."""
    batch = make_batch([
        make_variant("tiny_but_perfect", views=12, avg_watch_ms=10000, shares=5),
        make_variant("real_a", views=5000, avg_watch_ms=4000, shares=40),
        make_variant("real_b", views=4800, avg_watch_ms=3000, shares=20),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    assert ranking.winner.key == "real_a"
    tiny = next(s for s in ranking.scored if s.key == "tiny_but_perfect")
    assert not tiny.eligible
    assert "floor" in tiny.reason


def test_no_winner_when_not_enough_variants_have_data():
    batch = make_batch([
        make_variant("a", views=50, avg_watch_ms=5000),
        make_variant("b", views=10, avg_watch_ms=5000),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    assert ranking.winner is None
    assert not ranking.ready
    assert "floor" in ranking.summary


def test_near_tie_is_flagged_as_indecisive():
    batch = make_batch([
        make_variant("a", views=1000, avg_watch_ms=5000),
        make_variant("b", views=1000, avg_watch_ms=4950),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    assert ranking.winner is not None
    assert not ranking.decisive
    assert "near tie" in ranking.summary


def test_clear_lead_is_decisive():
    batch = make_batch([
        make_variant("a", views=1000, avg_watch_ms=9000, shares=90, saved=90),
        make_variant("b", views=1000, avg_watch_ms=3000, shares=5, saved=5),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    assert ranking.decisive
    assert (ranking.scored[0].score - ranking.scored[1].score) > DECISIVE_MARGIN


def test_retention_is_capped_at_full_watch():
    """Looping reels can report watch time above the clip length."""
    batch = make_batch([
        make_variant("looped", views=1000, avg_watch_ms=40000, duration=10.0),
        make_variant("normal", views=1000, avg_watch_ms=5000, duration=10.0),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)
    looped = next(s for s in ranking.scored if s.key == "looped")
    retention = next(c for c in looped.components if c.key == "retention")

    assert retention.raw == 1.0


def test_retention_falls_back_to_total_watch_time():
    variant = Variant(
        key="a", label="a", duration=10.0, ig_media_id="m", status="published",
        insights={"views": 100, "ig_reels_video_view_total_time": 500_000},
    )
    ranking = score_batch(make_batch([variant, variant]), WEIGHTS, min_audience=50)

    retention = next(c for c in ranking.scored[0].components if c.key == "retention")
    assert retention.raw == 0.5  # 500s total / 100 views = 5s avg of a 10s clip


def test_reach_used_when_views_missing():
    """Meta is retiring `reach`, but older media still only expose it."""
    variant = Variant(
        key="a", label="a", duration=10.0, ig_media_id="m", status="published",
        insights={"reach": 800, "ig_reels_avg_watch_time": 5000},
    )
    ranking = score_batch(make_batch([variant]), WEIGHTS, min_audience=300)
    assert ranking.scored[0].audience == 800


def test_variant_with_no_insights_is_ineligible_not_crashing():
    batch = make_batch([
        make_variant("good", views=1000, avg_watch_ms=5000),
        make_variant("good2", views=1000, avg_watch_ms=4000),
        Variant(key="dark", label="dark", ig_media_id="m", status="published", insights={}),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)
    dark = next(s for s in ranking.scored if s.key == "dark")

    assert not dark.eligible
    assert dark.reason == "no insight data yet"
    assert ranking.winner.key in {"good", "good2"}


def test_unpublished_variants_are_excluded():
    batch = make_batch([
        make_variant("published_a", views=1000, avg_watch_ms=5000),
        make_variant("published_b", views=1000, avg_watch_ms=4000),
        Variant(key="failed", label="failed", status="render_failed"),
    ])

    ranking = score_batch(batch, WEIGHTS, min_audience=300)
    assert {s.key for s in ranking.scored} == {"published_a", "published_b"}


def test_empty_batch_produces_no_winner():
    ranking = score_batch(make_batch([]), WEIGHTS, min_audience=300)
    assert ranking.winner is None
    assert "No variants" in ranking.summary


def test_report_tells_arda_exactly_what_to_do():
    batch = make_batch([
        make_variant("a", views=2000, avg_watch_ms=8000, shares=30),
        make_variant("b", views=2000, avg_watch_ms=3000, shares=4),
    ])
    batch.variants[0].permalink = "https://instagram.com/reel/AAA"
    ranking = score_batch(batch, WEIGHTS, min_audience=300)
    batch.winner_key = ranking.winner.key

    report = format_report(batch, ranking, graduation_strategy="MANUAL")

    assert "Herkesle paylas" in report
    assert "https://instagram.com/reel/AAA" in report
    assert batch.id in report


def test_report_for_auto_graduation_does_not_ask_for_a_tap():
    batch = make_batch([
        make_variant("a", views=2000, avg_watch_ms=8000),
        make_variant("b", views=2000, avg_watch_ms=3000),
    ])
    ranking = score_batch(batch, WEIGHTS, min_audience=300)
    batch.winner_key = ranking.winner.key

    report = format_report(batch, ranking, graduation_strategy="SS_PERFORMANCE")

    assert "Herkesle paylas" not in report
    assert "SS_PERFORMANCE" in report


def test_report_marks_ineligible_variants():
    batch = make_batch([
        make_variant("a", views=2000, avg_watch_ms=8000),
        make_variant("b", views=2000, avg_watch_ms=3000),
        make_variant("thin", views=20, avg_watch_ms=9000),
    ])
    ranking = score_batch(batch, WEIGHTS, min_audience=300)

    report = format_report(batch, ranking, graduation_strategy="MANUAL")
    assert "yeterli veri yok" in report
