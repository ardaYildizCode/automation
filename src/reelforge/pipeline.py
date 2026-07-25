"""Pipeline stages: publish -> measure -> graduate -> promote."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ai import LlmClient
from .adsmanager import AdsManager, format_review
from .config import REPO_ROOT, Config
from .director import direct
from .dropbox_client import Dropbox, DropboxFile
from .compositor import Compositor
from .editor import ensure_ffmpeg, probe
from .http import ApiError
from .instagram import Instagram
from .meta_ads import AdsError, MetaAds
from .notify import deliver
from .ranking import format_report, score_batch
from .state import Batch, BatchStatus, Store, Variant, parse_ts, utcnow

log = logging.getLogger(__name__)

# Enough for variety without downloading a whole library every run.
MAX_MUSIC_TRACKS = 12
PREVIEW_FOLDER = "/ReelForge/Onizleme"


class PipelineError(RuntimeError):
    pass


def make_llm(config: Config) -> LlmClient | None:
    """The AI layer is optional everywhere; absence degrades, never breaks."""
    if not config.ai_enabled:
        return None
    return LlmClient(
        api_key=config.llm_api_key,
        model=config.llm_model,
        base_url=config.llm_base_url,
    )


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
        for folder in (config.dropbox_inbox, config.dropbox_processed,
                       config.dropbox_renders, config.dropbox_music):
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

    from .director import default_treatments
    from .treatment import available_fonts

    fonts = available_fonts()
    print(f"[{'ok' if fonts else 'FAIL'}] {len(fonts)} bundled fonts: {', '.join(fonts)}")
    if not fonts:
        problems.append("No bundled fonts found under assets/fonts")

    fallback = default_treatments(config.variant_count, [])
    print(f"[ok] {len(fallback)} fallback treatments available")

    try:
        music = Dropbox(config).list_audio(config.dropbox_music)
        if music:
            print(f"[ok] {len(music)} music track(s) in {config.dropbox_music}")
        else:
            print(f"[WARN] {config.dropbox_music} is empty -> no music layer.")
            print("       Use tracks cleared for ads (e.g. Meta Sound Collection);")
            print("       unlicensed audio gets the ad rejected and flags the account.")
    except Exception as exc:
        print(f"[WARN] Could not read music folder: {exc}")

    print(f"[{'ok' if config.fal_api_key else 'WARN'}] "
          f"fal.ai generative {'configured' if config.fal_api_key else 'not configured (FAL_KEY unset)'}")

    if config.ai_enabled:
        client = make_llm(config)
        try:
            models = client.available_models()
            if config.llm_model in models:
                print(f"[ok] LLM: {config.llm_model} (ads mode: {config.ai_ads_mode})")
            else:
                problems.append(f"LLM model {config.llm_model} not offered by the provider")
                print(f"[FAIL] LLM model {config.llm_model!r} is not available.")
                close = [m for m in models if "claude" in m][:5]
                if close:
                    print(f"       try one of: {', '.join(close)}")
        except Exception as exc:
            problems.append(f"LLM: {exc}")
            print(f"[FAIL] LLM unreachable: {exc}")
    else:
        print("[WARN] OPENROUTER_API_KEY unset -> fixed recipes, no AI ad review")

    from .editor import find_font, has_drawtext
    if not (find_font() and has_drawtext()):
        print("[WARN] drawtext or font unavailable -> text variants render plain")

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
    for folder in (config.dropbox_inbox, config.dropbox_processed,
                   config.dropbox_renders, config.dropbox_music):
        dropbox.ensure_folder(folder)

    source = _next_source(dropbox, store, config)
    if source is None:
        log.info("No new media in %s", config.dropbox_inbox)
        return 0

    instagram = Instagram(config)
    trial, detail = instagram.trial_reels_available()
    log.info("Trial reels %s: %s", "enabled" if trial else "DISABLED", detail)

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

        # Licensed music beds only. Meta's rights system rejects an ad whose
        # audio is not cleared and flags the account, so the tracks come from
        # a folder Arda fills rather than from anywhere automatic.
        music_dir = workdir / "music"
        tracks = _fetch_music(dropbox, config, music_dir)

        # The model looks at real frames and decides how to cut this clip;
        # without a key it silently falls back to the default catalogue.
        direction = direct(
            make_llm(config),
            local_source,
            info,
            workdir,
            filename=source.name,
            variant_count=config.variant_count,
            fallback_caption=build_caption(config, source.name),
            music_tracks=tracks,
        )
        treatments = direction.treatments
        caption = direction.caption
        batch.direction = {
            "source": direction.source,
            "product": direction.product_name,
            "observations": direction.observations,
            "rationales": direction.rationales,
            "caption": caption,
            "treatments": [t.to_dict() for t in direction.treatments],
        }
        if direction.product_name:
            batch.product = direction.product_name
        batch.notes.append(
            "Variants chosen by AI art direction"
            if direction.ai_generated
            else "Variants from the default catalogue (no AI)"
        )

        renderer = Compositor(workdir / "out", music_dir if tracks else None)
        for treatment in treatments:
            variant = Variant(
                key=treatment.key,
                label=treatment.label,
                recipe=treatment.summary(),
            )
            batch.variants.append(variant)
            try:
                rendered = renderer.render(local_source, treatment, info, batch.id)
                variant.duration = probe(rendered).duration
                variant.render_name = rendered.name
            except Exception as exc:
                variant.status = "render_failed"
                variant.error = str(exc)[:500]
                log.error("Variant %s failed to render: %s", treatment.key, exc)
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
                log.error("Variant %s failed to upload: %s", treatment.key, exc)
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


def _fetch_music(dropbox: Dropbox, config: Config, destination: Path) -> list[str]:
    """Download the licensed music library once per batch."""
    try:
        tracks = dropbox.list_audio(config.dropbox_music)
    except Exception as exc:  # noqa: BLE001 - music is optional
        log.warning("Could not list music folder: %s", exc)
        return []

    if not tracks:
        log.info("No music in %s; treatments render without a bed", config.dropbox_music)
        return []

    names: list[str] = []
    for track in tracks[:MAX_MUSIC_TRACKS]:
        try:
            dropbox.download(track.path, destination / track.name)
            names.append(track.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not download %s: %s", track.name, exc)
    log.info("Music library: %d track(s)", len(names))
    return names


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


def run_graduate(
    config: Config, store: Store, batch_id: str, *, also: list[str] | None = None
) -> int:
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
    for key in [batch.winner_key, *(also or [])]:
        if not key:
            continue
        if batch.variant(key) is None:
            raise PipelineError(f"Batch {batch_id} has no variant {key!r}")
        if key not in batch.graduated_keys:
            batch.graduated_keys.append(key)
    store.save()

    winner = batch.winner
    log.info(
        "Batch %s marked graduated; winner %s (%s)",
        batch_id, batch.winner_key, winner.permalink if winner else "",
    )
    return 0


# ---------------------------------------------------------------- promote


def run_promote(
    config: Config,
    store: Store,
    batch_id: str,
    *,
    budget: int | None = None,
    ab_cells: int = 2,
) -> int:
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

    name_prefix = f"RF {batch.product or batch.id}"[:60]

    # Only variants that were graduated are visible to followers; a trial reel
    # behind an ad would be shown to an audience that cannot see the post.
    contenders = [
        (v.key, v.ig_media_id)
        for v in batch.top_variants(ab_cells)
        if v.key in batch.graduated_keys and v.ig_media_id
    ]

    if len(contenders) >= 2:
        test = ads.create_ab_test(
            contenders=contenders,
            name_prefix=name_prefix,
            daily_budget_try=budget,
        )
        batch.ad = test.to_dict()
        lines = [
            f"# A/B reklam testi kuruldu - `{batch.id}`",
            "",
            f"- Kampanya: `{test.campaign_id}`",
            f"- Hucre basina butce: **{test.cell_budget_try} TRY/gun**",
            "",
            "| Varyant | Ad set | Reklam | Gonderi |",
            "|---|---|---|---|",
        ]
        for cell in test.cells:
            variant = batch.variant(cell["variant_key"])
            link = variant.permalink if variant else ""
            lines.append(
                f"| {variant.label if variant else cell['variant_key']} "
                f"| `{cell['adset_id']}` | `{cell['ad_id']}` | {link} |"
            )
        warnings = test.warnings
    else:
        chain = ads.create_ad_from_ig_post(
            ig_media_id=winner.ig_media_id,
            name_prefix=name_prefix,
            daily_budget_try=budget,
        )
        batch.ad = chain.to_dict()
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
            "Tek kazanan mezun edildigi icin A/B yerine tek reklam kuruldu. "
            "A/B icin en az iki varyanti mezun et.",
        ]
        warnings = chain.warnings

    batch.status = BatchStatus.PROMOTED
    store.save()

    lines += ["", "**Hepsi PAUSED.**"]
    if warnings:
        lines += ["", "## Dikkat"] + [f"- {w}" for w in warnings]

    deliver(config, f"{batch.id}-ad", "\n".join(l for l in lines if l))
    log.info("Batch %s promoted", batch_id)
    return 0


# ---------------------------------------------------------------- preview


def run_preview(config: Config, *, source_name: str = "", count: int = 0) -> int:
    """Render treatments to Dropbox and publish nothing.

    The cheapest way to see what the pipeline would post: needs only Dropbox
    credentials, touches no Instagram or Meta endpoint, and writes no state.
    """
    ensure_ffmpeg()
    dropbox = Dropbox(config)
    for folder in (config.dropbox_inbox, config.dropbox_music, PREVIEW_FOLDER):
        dropbox.ensure_folder(folder)

    candidates = dropbox.list_media(config.dropbox_inbox)
    if not candidates:
        raise PipelineError(
            f"No media in {config.dropbox_inbox}. Put a video or photo there first."
        )

    if source_name:
        source = next((c for c in candidates if c.name.lower() == source_name.lower()), None)
        if source is None:
            available = ", ".join(c.name for c in candidates[:10])
            raise PipelineError(f"{source_name!r} not found. Available: {available}")
    else:
        source = candidates[-1]  # newest, which is what you just dropped in

    wanted = count or config.variant_count
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    target = f"{PREVIEW_FOLDER}/{stamp}"
    dropbox.ensure_folder(target)

    log.info("Previewing %s -> %s", source.name, target)
    rendered_names: list[str] = []

    with tempfile.TemporaryDirectory(prefix="reelforge-preview-") as tmp:
        workdir = Path(tmp)
        local_source = dropbox.download(source.path, workdir / source.name)
        info = probe(local_source)

        music_dir = workdir / "music"
        tracks = _fetch_music(dropbox, config, music_dir)

        direction = direct(
            make_llm(config), local_source, info, workdir,
            filename=source.name,
            variant_count=wanted,
            fallback_caption=build_caption(config, source.name),
            music_tracks=tracks,
        )

        compositor = Compositor(workdir / "out", music_dir if tracks else None)
        for treatment in direction.treatments:
            try:
                rendered = compositor.render(local_source, treatment, info, "onizleme")
            except Exception as exc:  # noqa: BLE001 - report and keep going
                log.error("%s failed: %s", treatment.key, exc)
                continue
            dropbox.upload(rendered, f"{target}/{rendered.name}")
            rendered_names.append(rendered.name)

        notes = [
            f"# Onizleme {stamp}",
            "",
            f"Kaynak: {source.name}",
            f"Yonlendirme: {'AI' if direction.ai_generated else 'varsayilan katalog'}",
            f"Muzik: {len(tracks)} parca" if tracks else "Muzik: yok",
            "",
            "## Caption",
            "",
            direction.caption,
            "",
            "## Varyantlar",
            "",
        ]
        for treatment in direction.treatments:
            notes.append(f"- **{treatment.key}** - {treatment.summary()}")
            if treatment.rationale:
                notes.append(f"  - {treatment.rationale}")
        report = "\n".join(notes)

        summary = workdir / "OKUBENI.md"
        summary.write_text(report, encoding="utf-8")
        dropbox.upload(summary, f"{target}/OKUBENI.md")

    deliver(config, f"preview-{stamp}", report)
    print(f"\n{len(rendered_names)} varyant hazir: Dropbox {target}")
    print("Telefonundan Dropbox uygulamasiyla izleyebilirsin. Instagram'a hicbir sey gitmedi.")
    return 0


# ---------------------------------------------------------------- ads review


def run_ads_review(config: Config, store: Store, *, apply: bool | None = None) -> int:
    """AI reads the whole account and proposes actions; rules decide."""
    if config.ai_ads_mode == "off":
        log.info("AI_ADS_MODE=off; skipping.")
        return 0

    should_apply = config.ai_ads_mode == "apply" if apply is None else apply
    try:
        manager = AdsManager(config, make_llm(config))
    except AdsError as exc:
        raise PipelineError(str(exc)) from exc

    review = manager.review(apply=should_apply)
    report = format_review(review)
    deliver(config, f"ads-{datetime.now(timezone.utc):%Y%m%d-%H%M}", report)

    log.info(
        "Ads review: %d proposed, %d accepted, %d applied",
        len(review.proposals), len(review.accepted),
        sum(1 for p in review.accepted if p.applied),
    )
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
