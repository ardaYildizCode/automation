# ReelForge

Drop one video or photo into Dropbox. Ten professionally finished variants get
rendered, published as Instagram **trial reels** (visible only to non-followers),
ranked on real retention data, and the winner becomes a Meta ad.

```
Dropbox inbox
   -> 10 ffmpeg variants (1080x1920, Rec.709, loudness-matched)
   -> 10 trial reels on @annekiz.store
   -> 24h later: insights pulled, variants ranked
   -> report tells you which one to graduate
   -> you tap "Share with everyone" in the app
   -> paused ad built from the winner
```

Runs entirely on GitHub Actions. Nothing needs your PC to be on, and it does not
need ffmpeg or Python working locally.

---

## The one thing that is not automated

**Instagram has no API to graduate a trial reel into a normal reel.** Meta
exposes `trial_params.graduation_strategy` when you *create* the reel, and that
is all:

| Strategy | What happens |
|---|---|
| `MANUAL` (default here) | Stays a trial until you tap **Share with everyone** in the app. |
| `SS_PERFORMANCE` | Meta auto-graduates whatever *it* judges to be performing. It ignores your ranking, and may graduate several or none. |

So the pipeline does everything up to and after that tap, and tells you exactly
which reel to tap. If you would rather Meta decide, set the repository variable
`TRIAL_GRADUATION_STRATEGY=SS_PERFORMANCE`.

Trial reels also require **1,000+ followers**. `@annekiz.store` clears this
comfortably; the pipeline still re-checks at runtime and falls back to publishing
normal reels rather than failing.

---

## Setup

### 1. Instagram / Meta token

The existing Meta connector is ads-only and cannot publish organically, so this
needs its own token.

Use a **System User token** from Business Manager. Unlike a user token it does
not expire, which matters for something running on a schedule.

1. [business.facebook.com](https://business.facebook.com) -> Business settings
   -> Users -> **System users** -> Add, role **Admin**.
2. **Add assets**: the ad account `563100009538815` and the page
   `512766308577499`, both with full control.
3. **Generate new token**, pick your app, and select these permissions:

   ```
   instagram_basic
   instagram_content_publish
   instagram_manage_insights
   pages_show_list
   pages_read_engagement
   business_management
   ads_management
   ```
4. Copy the token. Verify it before saving it anywhere:

   ```bash
   curl -s "https://graph.facebook.com/v25.0/17841415328875109\
?fields=username,followers_count&access_token=YOUR_TOKEN"
   ```
   You should get back `annekiz.store` and the follower count.

> If you use a normal user token instead, it expires after 60 days and the
> pipeline will start failing silently at that point. Prefer the system user.

### 2. Dropbox refresh token

Short-lived Dropbox tokens die after 4 hours, so the pipeline uses a refresh
token.

1. Create an app at [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
   (Scoped access, Full Dropbox). Under **Permissions** enable
   `files.content.read`, `files.content.write`, `files.metadata.read`.
2. Visit this URL, approve, and copy the code:

   ```
   https://www.dropbox.com/oauth2/authorize?client_id=APP_KEY&response_type=code&token_access_type=offline
   ```
3. Exchange it (the code is single use):

   ```bash
   curl -s https://api.dropbox.com/oauth2/token \
     -d code=THE_CODE -d grant_type=authorization_code \
     -u APP_KEY:APP_SECRET
   ```
   Save the `refresh_token` from the response.

### 3. GitHub secrets and variables

**Settings -> Secrets and variables -> Actions**

Secrets:

| Name | Value |
|---|---|
| `IG_ACCESS_TOKEN` | from step 1 |
| `IG_USER_ID` | `17841415328875109` |
| `DROPBOX_APP_KEY` | from step 2 |
| `DROPBOX_APP_SECRET` | from step 2 |
| `DROPBOX_REFRESH_TOKEN` | from step 2 |
| `TELEGRAM_BOT_TOKEN` | optional, for phone notifications |
| `TELEGRAM_CHAT_ID` | optional |

Variables (all optional, sensible defaults apply):

| Name | Default | Notes |
|---|---|---|
| `META_AD_ACCOUNT_ID` | — | `563100009538815`. Without it, `promote` is disabled. |
| `FACEBOOK_PAGE_ID` | — | `512766308577499`. Same. |
| `TRIAL_GRADUATION_STRATEGY` | `MANUAL` | or `SS_PERFORMANCE` |
| `MEASURE_AFTER_HOURS` | `24` | how long trials run before ranking |
| `MIN_REACH_PER_VARIANT` | `300` | views a variant needs before it can win |
| `VARIANT_COUNT` | `10` | |
| `MAX_BATCHES_PER_DAY` | `1` | guard against burning through the inbox |
| `AD_DAILY_BUDGET_TRY` | `150` | forced up to 100 minimum |
| `DROPBOX_INBOX` | `/ReelForge/Gelen` | |

### 4. Dropbox folders

Created automatically on first run, or make them yourself:

```
/ReelForge/Gelen       <- drop new footage here
/ReelForge/Islenen     <- moved here once published
/ReelForge/Varyantlar  <- the 10 renders, kept for reference
```

Name files `product_price_sizes.mp4` (e.g. `elbise_450_2-8yas.mp4`) and the
caption fills itself in. Unstructured names still work.

### 5. Verify

Actions -> **Publish trial reels** -> Run workflow with **dry run** ticked. It
renders all ten and publishes nothing. Or locally:

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python -m reelforge check
```

---

## Daily use

| When | What |
|---|---|
| You | Drop footage into `/ReelForge/Gelen` |
| 09:00 TR, automatic | Ten trial reels go up |
| +24h, automatic | Ranked; report lands in `reports/` and on Telegram |
| You | Tap **Share with everyone** on the winner |
| You | Actions -> **Graduate and promote** -> paste the batch id |

The ad is created **paused**, and API-created campaigns appear in Ads Manager as
unpublished drafts behind **Review and Publish**. Nothing spends without you.

Commands, if you prefer the CLI:

```bash
python -m reelforge check                 # verify everything is wired up
python -m reelforge publish               # render + publish a batch
python -m reelforge measure --force       # rank now, ignoring the age threshold
python -m reelforge status                # what is in flight
python -m reelforge graduate <batch-id>   # record that you tapped it
python -m reelforge promote <batch-id>    # build the paused ad
```

---

## The ten variants

Tunable in [`variants.yaml`](variants.yaml). `control` must stay first — without
an untouched baseline you cannot tell whether an edit helped or merely differed.
All ten carry the **same caption**, so the edit is the only variable.

| # | Key | Change |
|--:|---|---|
| 1 | `control` | Untouched baseline |
| 2 | `hook_trim` | First 1.5s cut — faster hook |
| 3 | `speed_up` | 8% faster |
| 4 | `zoom_punch` | Slow push-in |
| 5 | `bright_pop` | Brighter, slightly more saturated |
| 6 | `warm_grade` | Warm tone |
| 7 | `clean_grade` | Cool, neutral tone |
| 8 | `text_hook` | Typographic hook over the first 3s |
| 9 | `freeze_open` | 0.8s freeze on the opening frame |
| 10 | `tight_crop` | Tighter framing |

### Finishing applied to every variant

Identical across all ten, so it lifts the floor rather than biasing the test:

- **1080x1920 over a blurred fill** of the footage itself, so landscape and
  square sources go full-bleed instead of sitting in black bars.
- **Rec.709 stamped onto the frames**, not just set as output flags. This is
  what prevents the washed-out look after Instagram re-encodes.
- **Unsharp pass** to restore the micro-contrast scaling costs — it is what
  makes fabric texture and stitching read on a phone.
- **0.25s fade** top and tail instead of a hard camera-roll cut.
- **Loudness normalised to -14 LUFS**, Instagram's own target, so variants do
  not differ in perceived volume.
- **CRF 19, `medium` preset, 48kHz stereo AAC, faststart.**
- Clips under Instagram's 3s minimum are **looped** to clear it; anything over
  90s is trimmed.

### Why the grades are subtle

You sell by colour — customers order a garment in a colour they saw in a video.
A punchy, saturated render that misrepresents the fabric buys returns, not
sales. Saturation is capped at 1.15 and there is a test enforcing it. Push the
numbers in `variants.yaml` only after checking a render against the real
garment.

---

## How the winner is picked

Ranked on what predicts a creative surviving contact with ad spend, not on
vanity counts:

| Signal | Weight | Why |
|---|--:|---|
| Watch-through rate | 40% | The single best predictor of a reel that keeps working |
| Shares / view | 25% | Genuine intent, hardest to fake |
| Saves / view | 20% | Purchase consideration |
| Likes + comments / view | 15% | Weakest signal, weighted accordingly |

Each signal is normalised against the best in the *same batch*, so the score
answers "which of these ten", not "is this good in the abstract".

A variant needs `MIN_REACH_PER_VARIANT` views before it can win, so a lucky
12-view reel cannot beat a 5,000-view one. If fewer than two variants clear the
floor, no winner is declared and the next scheduled run re-measures. When the
leader is within 8% of the runner-up the report says so rather than pretending
the result is decisive.

---

## Notes on this ad account

Encoded in the code because they were learned the hard way:

- `ads_boost_ig_post` does not work here — the explicit
  campaign -> ad set -> creative -> ad chain is used instead.
- The creative uses `source_instagram_media_id`, so the ad inherits the
  organic post's likes and comments rather than starting cold.
- Ad sets below 100 TRY/day never reach the 50-results-in-7-days learning
  threshold, so the budget is forced up to that floor.
- `advantage_audience: 0` is set, otherwise Meta treats the age bounds as a
  suggestion.
- Targeting defaults to women 25-44 in Turkey, the account's most efficient
  segment.

## State

`state/batches.json` is the pipeline's memory, committed back by the workflows —
no database, and full history in git. `reports/` holds every ranking report.

## Tests

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

99 tests. The editor ones render real media through ffmpeg, because filtergraph
mistakes otherwise surface at 06:00 UTC rather than in CI.
