"""Pipeline stages: publish -> measure -> graduate -> promote."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import REPO_ROOT, Config
from .dropbox_client import Dropbox, DropboxFile
from .editor import VariantRenderer, ensure_ffmpeg, load_recipes, probe
from .http import ApiError
from .instagram import Instagram
from .meta_ads import AdsError, MetaAds
from .notify import deliver
from .ranking import format_report, score_batch
from .state import Batch, BatchStatus, Store, Variant, parse_ts, utcnow

log = logging.getLogger(__name__)

RECIPES_PATH = REPO_ROOT / "variants.yaml"


class PipelineError(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers


def parse_source_name(filename: str) -> dict[str, str]:
    """Read `urun_fiyat_bedenler.mp4` into caption fields.

    Every part is optional; an unstructured filename just yields a product
    name and empty extras rather than failing the run.
    """
    stem = Path(filename).stem
    parts = [p.strip() for p in re.split(r"[_-]+", stem) if p.strip()]
    fields = {"product": "", "price": "", "sizes": ""}

    if parts:
        fields["product"] = parts[0].replace(".", " ").strip()
    if len(parts) > 1 and re.search(r"\d", parts[1]):
        fields["price"] = parts[1]
    if len(parts) > 2:
        fields["sizes"] = " ".join(parts[2:])
    return fields


def build_caption(config: Config, source_name: str) -> str:
    fields = parse_source_name(source_name)
    caption = config.caption_template.format(
        product=fields["product"] or "Yeni model",
        price=fields["price"],
        sizes=fields["sizes"],
        whatsapp=config.whatsapp_number,
    )
    if fields["price"]:
        caption = caption.replace("{price}", fields["price"])
    return caption.strip()


def new_batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _daily_quota_reached(store: Store, config: Config) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return len(store.batches_created_since(since)) >= config.max_batches_per_day


# ---------------------------------------------------------------- check


def run_check(config: Config) -> int:
    """Pre-flight: prove every dependency works before trusting the schedule."""
    problems: list[str] = []

    try:
        ensure_ffmpeg()
        print("[ok] ffmpeg and ffprobe available")
    except Exception as exc:
        problems.append(str(exc))
        print(f"[FAIL] {exc}")

    dropbox = Dropbox(config)
    try:
        for folder in (config.dropbox_inbox, config.dropbox_processed, config.dropbox_renders):
            dropbox.ensure_folder(folder)
        pending = dropbox.list_media(config.dropbox_inbox)
        print(f"[ok] Dropbox reachable; {len(pending)} media file(s) in {config.dropbox_inbox}")
    except Exception as exc:
        problems.append(f"Dropbox: {exc}")
        print(f"[FAIL] Dropbox: {exc}")

    instagram = Instagram(config)
    try:
        available, detail = instagram.trial_reels_available()
        print(f"[{'ok' if available else 'WARN'}] Instagram: {detail}")
        if not available:
            print("       -> pipeline will publish normal reels instead of trials")
    except Exception as exc:
        problems.append(f"Instagram: {exc}")
        print(f"[FAIL] Instagram: {exc}")

    if config.ads_requirements_met():
        print(f"[ok] Ads configured (account {config.ad_account_id}, page {config.facebook_page_id})")
    else:
        print("[WARN] META_AD_ACCOUNT_ID / FACEBOOK_PAGE_ID unset -> `promote` disabled")

    recipes = load_recipes(RECIPES_PATH, config.variant_count)
    print(f"[ok] {len(recipes)} variant recipes loaded: {', '.join(r.key for r in recipes)}")

    if problems:
        print(f"\n{len(problems)} blocking problem(s) found.")
        return 1
    print("\nAll checks passed.")
    return 0


# ---------------------------------------------------------------- publish


def run_publish(config: Config, store: Store) -> int:
    ensure_ffmpeg()

    if _daily_quota_reached(store, config):
        log.info("Daily batch quota (%d) already used; nothing to do.", config.max_batches_per_day)
        return 0

    dropbox = Dropbox(config)
    for folder in (config.dropbox_inbox, config.dropbox_processed, config.dropbox_renders):
        dropbox.ensure_folder(folder)

    source = _next_source(dropbox, store, config)
    if source is None:
        log.info("No new media in %s", config.dropbox_inbox)
        return 0

    instagram = Instagram(config)
    trial, detail = instagram.trial_reels_available()
    log.info("Trial reels %s: %s", "enabled" if trial else "DISABLED", detail)

    recipes = load_recipes(RECIPES_PATH, config.variant_count)
    caption = build_caption(config, source.name)
    batch = Batch(
        id=new_batch_id(),
        source_path=source.path,
        source_name=source.name,
        source_hash=source.content_hash,
        product=parse_source_name(source.name)["product"],
        created_at=utcnow(),
    )
    if not trial:
        batch.notes.append(f"Published as normal reels, not trials: {detail}")

    with tempfile.TemporaryDirectory(prefix="reelforge-") as tmp:
        workdir = Path(tmp)
        local_source = dropbox.download(source.path, workdir / source.name)
        info = probe(local_source)
        log.info(
            "Source %s: %dx%d, %.1fs, audio=%s",
            source.name, info.width, info.height, info.duration, info.has_audio,
        )

        renderer = VariantRenderer(workdir / "out")
        for recipe in recipes:
            variant = Variant(key=recipe.key, label=recipe.label, recipe=recipe.kind)
            batch.variants.append(variant)
            try:
                rendered = renderer.render(local_source, recipe, info, batch.id)
                variant.duration = probe(rendered).duration
                variant.render_name = rendered.name
            except Exception as exc:
                variant.status = "render_failed"
                variant.error = str(exc)[:500]
                log.error("Variant %s failed to render: %s", recipe.key, exc)
                continue

            if config.dry_run:
                variant.status = "rendered_dry_run"
                continue

            try:
                dest = f"{config.dropbox_renders.rstrip('/')}/{batch.id}/{rendered.name}"
                dropbox.upload(rendered, dest)
                # Instagram fetches the video itself, so it needs a public URL.
                video_url = dropbox.temporary_link(dest)
            except Exception as exc:
                variant.status = "upload_failed"
                variant.error = str(exc)[:500]
                log.error("Variant %s failed to upload: %s", recipe.key, exc)
                continue

            result = instagram.publish_reel(
                video_url,
                caption,
                trial=trial,
                graduation_strategy=config.trial_graduation_strategy,
            )
            variant.ig_container_id = result.container_id
            if result.ok:
                variant.ig_media_id = result.media_id
                variant.permalink = result.permalink
                variant.status = "published"
            else:
                variant.status = "publish_failed"
                variant.error = result.error

    published = len(batch.published_variants)
    if config.dry_run:
        batch.status = BatchStatus.PUBLISHING
        batch.notes.append("DRY_RUN: rendered only, nothing published")
    elif published == 0:
        batch.status = BatchStatus.FAILED
        batch.notes.append("No variant reached Instagram")
    else:
        batch.status = BatchStatus.PUBLISHED
        batch.published_at = utcnow()

    store.add(batch)
    store.save()

    if not config.dry_run and published:
        dropbox.move(
            source.path,
            f"{config.dropbox_processed.rstrip('/')}/{source.name}",
        )

    log.info("Batch %s: %d/%d variants published", batch.id, published, len(batch.variants))
    return 0 if published or config.dry_run else 1


def _next_source(dropbox: Dropbox, store: Store, config: Config) -> DropboxFile | None:
    for candidate in dropbox.list_media(config.dropbox_inbox):
        if candidate.content_hash and store.already_seen(candidate.content_hash):
            log.info("Skipping %s: already processed", candidate.name)
            continue
        return candidate
    return None


# ---------------------------------------------------------------- measure


def run_measure(config: Config, store: Store, *, force: bool = False) -> int:
    instagram = Instagram(config)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.measure_after_hours)
    handled = 0

    for batch in list(store.by_status(BatchStatus.PUBLISHED, BatchStatus.MEASURED)):
        anchor = parse_ts(batch.published_at or batch.created_at)
        if not force and anchor > cutoff:
            log.info(
                "Batch %s is only %.1fh old; waiting for %dh",
                batch.id,
                (datetime.now(timezone.utc) - anchor).total_seconds() / 3600,
                config.measure_after_hours,
            )
            continue

        for variant in batch.published_variants:
            try:
                variant.insights = instagram.insights(variant.ig_media_id)
            except ApiError as exc:
                log.warning("Insights failed for %s: %s", variant.key, exc)

        ranking = score_batch(batch, config.scoring_weights, config.min_reach_per_variant)
        batch.measured_at = utcnow()
        if ranking.winner:
            batch.winner_key = ranking.winner.key
            batch.status = BatchStatus.MEASURED
        else:
            # Leave it PUBLISHED so the next scheduled run re-measures it.
            batch.status = BatchStatus.PUBLISHED

        report = format_report(
            batch, ranking, graduation_strategy=config.trial_graduation_strategy
        )
        deliver(config, batch.id, report)
        handled += 1
        log.info("Batch %s measured: %s", batch.id, ranking.summary)

    store.save()
    if handled == 0:
        log.info("No batches were due for measurement.")
    return 0


# ---------------------------------------------------------------- graduate


def run_graduate(config: Config, store: Store, batch_id: str) -> int:
    """Record that the winner was graduated by hand in the Instagram app.

    There is no Graph API endpoint for this, so the pipeline tracks the fact
    rather than performing it.
    """
    batch = store.get(batch_id)
    if batch is None:
        raise PipelineError(f"Unknown batch {batch_id!r}")
    if not batch.winner_key:
        raise PipelineError(
            f"Batch {batch_id} has no winner yet. Run `measure` first."
        )

    batch.status = BatchStatus.GRADUATED
    batch.graduated_at = utcnow()
    store.save()

    winner = batch.winner
    log.info(
        "Batch %s marked graduated; winner %s (%s)",
        batch_id, batch.winner_key, winner.permalink if winner else "",
    )
    return 0


# ---------------------------------------------------------------- promote


def run_promote(config: Config, store: Store, batch_id: str, *, budget: int | None = None) -> int:
    batch = store.get(batch_id)
    if batch is None:
        raise PipelineError(f"Unknown batch {batch_id!r}")

    winner = batch.winner
    if winner is None or not winner.ig_media_id:
        raise PipelineError(f"Batch {batch_id} has no published winner to promote.")

    if batch.status != BatchStatus.GRADUATED:
        raise PipelineError(
            f"Batch {batch_id} is {batch.status!r}, not graduated. "
            "A trial reel is only visible to non-followers, so promote it after "
            "graduating it in the app, then run `graduate` to record that."
        )

    if batch.ad.get("ad_id"):
        log.info("Batch %s already promoted as ad %s", batch_id, batch.ad["ad_id"])
        return 0

    try:
        ads = MetaAds(config)
    except AdsError as exc:
        raise PipelineError(str(exc)) from exc

    chain = ads.create_ad_from_ig_post(
        ig_media_id=winner.ig_media_id,
        name_prefix=f"RF {batch.product or batch.id}"[:60],
        daily_budget_try=budget,
    )
    batch.ad = chain.to_dict()
    batch.status = BatchStatus.PROMOTED
    store.save()

    lines = [
        f"# Reklam kuruldu - `{batch.id}`",
        "",
        f"Kazanan varyant: **{winner.label}** (`{winner.key}`)",
        f"Gonderi: {winner.permalink}" if winner.permalink else "",
        "",
        f"- Kampanya: `{chain.campaign_id}`",
        f"- Ad set: `{chain.adset_id}`",
        f"- Kreatif: `{chain.creative_id}`",
        f"- Reklam: `{chain.ad_id}`",
        "",
        "**Hepsi PAUSED.**",
    ]
    if chain.warnings:
        lines += ["", "## Dikkat"] + [f"- {w}" for w in chain.warnings]

    deliver(config, f"{batch.id}-ad", "\n".join(l for l in lines if l is not None))
    log.info("Batch %s promoted: ad %s", batch_id, chain.ad_id)
    return 0


# ---------------------------------------------------------------- status


def run_status(config: Config, store: Store) -> int:
    if not store.batches:
        print("No batches yet.")
        return 0

    print(f"{'BATCH':<24} {'STATUS':<12} {'PUB':>5} {'WINNER':<16} SOURCE")
    for batch in sorted(store.batches, key=lambda b: b.created_at, reverse=True)[:20]:
        print(
            f"{batch.id:<24} {batch.status:<12} "
            f"{len(batch.published_variants):>2}/{len(batch.variants):<2} "
            f"{batch.winner_key or '-':<16} {batch.source_name}"
        )

    pending = [b for b in store.batches if b.status == BatchStatus.MEASURED]
    if pending:
        print("\nWaiting for you to graduate in the Instagram app:")
        for batch in pending:
            winner = batch.winner
            print(f"  {batch.id}: {winner.label if winner else '?'} -> {winner.permalink if winner else ''}")
    return 0
