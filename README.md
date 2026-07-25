# ReelForge

Drop one video or photo into Dropbox. An AI art director looks at the actual
footage and decides how to cut it, ten professionally finished variants get
rendered and published as Instagram **trial reels** (visible only to
non-followers), ranked on real retention data, and the winners go head to head
as a Meta A/B test.

```
Dropbox inbox
   -> AI looks at real frames and writes 10 full treatments:
      motion + grade + hook copy/font/colour/style + music bed
   -> 10 ffmpeg renders (1080x1920, Rec.709, loudness-matched)
   -> 10 trial reels on @annekiz.store
   -> 24h later: insights pulled, variants ranked
   -> report tells you which ones to graduate
   -> you tap "Share with everyone"
   -> paused A/B ad test built from the graduated winners
```

Separately, a daily AI review reads the whole ad account and proposes budget
and pause actions, which a rule engine vets before anything executes.

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

## Where AI is used, and where it deliberately is not

| Layer | Who decides | Why |
|---|---|---|
| The whole creative treatment: motion, grade, hook copy/font/colour/style, music choice, cover frame | **AI** | Needs judgement about footage. Fixed recipes cannot tell a dark clip from a bright one, or which hook line suits this garment. |
| Rendering, ranking, spend rules | **Code** | Already deterministic and known. A model adds risk here, not accuracy. |
| Ad account actions | **AI proposes, code decides** | The model is good at reading a whole account at once. It is not something to give unsupervised write access to a live budget. |

Every AI output is treated as untrusted input. Recipe kinds must exist in the
catalogue, numeric parameters are clamped, hook text is sanitised for
`drawtext`, and ad proposals are rejected unless they clear every spend rule.
If `OPENROUTER_API_KEY` is missing the whole system falls back to the fixed
recipes and keeps working.

### The ad rule engine

An AI proposal is **rejected** unless it passes all of these:

- The entity is not on the protected list (the purchase campaign stays off
  until the pixel is fixed).
- The entity actually exists in the snapshot — guards against invented ids.
- **Pause** needs spend >= 400 TRY *and* >= 30 results *and* cost above 1.5x
  the account average. The one exception is a structurally broken ad set —
  real spend with zero results — which can be paused during learning.
  "Expensive" and "broken" are different problems.
- No pausing or budget-cutting an ad set still inside its learning period
  (under 50 results).
- **Budget changes** are capped at ±25% per run, never below the 100 TRY/day
  learning threshold, and never past the account daily cap.
- At most 6 actions per run.

`AI_ADS_MODE` controls what happens next: `propose` (default — reports only),
`apply` (executes what passes), `off`.

Start on `propose` and read a week of reports before switching. The rules are
enforced either way, but on `propose` you see what it *would* have done.

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
| `OPENROUTER_API_KEY` | from [openrouter.ai/keys](https://openrouter.ai/keys) — powers the art director and ad review |
| `FAL_KEY` | optional, from [fal.ai](https://fal.ai) — generative video |
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
| `DROPBOX_MUSIC` | `/ReelForge/Muzik` | ad-licensed tracks only |
| `LLM_MODEL` | `anthropic/claude-sonnet-4.5` | Any vision-capable OpenRouter model. `check` verifies it exists. |
| `AI_ADS_MODE` | `propose` | `propose`, `apply` or `off` |
| `MAX_ACCOUNT_DAILY_BUDGET_TRY` | `500` | hard ceiling the AI cannot push past |
| `PROTECTED_ENTITY_IDS` | the two known-dangerous campaigns | comma separated, never touched |

### 4. Dropbox folders

Created automatically on first run, or make them yourself:

```
/ReelForge/Gelen       <- drop new footage here
/ReelForge/Muzik       <- ad-licensed music beds (see the music section)
/ReelForge/Islenen     <- moved here once published
/ReelForge/Varyantlar  <- the 10 renders, kept for reference
```

Name files `product_price_sizes.mp4` (e.g. `elbise_450_2-8yas.mp4`) and the
caption fills itself in. Unstructured names still work.

### 5. See it before trusting it

The preview path needs **only the Dropbox secrets** — no Instagram token, no ad
account. It renders the treatments into Dropbox and publishes nothing.

1. Put a video or photo in `/ReelForge/Gelen`
2. Actions -> **Onizleme (test)** -> Run workflow
3. Open `/ReelForge/Onizleme/<date>/` in the Dropbox app and watch them

`OKUBENI.md` in that folder lists what each variant changed and the caption
that would have been posted.

Add `OPENROUTER_API_KEY` and the same preview runs through the AI art director
instead of the built-in catalogue, so you can compare the two before spending
anything.

Once the Instagram token exists, verify the rest:

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python -m reelforge check      # credentials, fonts, drawtext, music, model
python -m reelforge preview    # same as the workflow, run locally
```

---

## Daily use

| When | What |
|---|---|
| You | Drop footage into `/ReelForge/Gelen` |
| 09:00 TR, automatic | Ten trial reels go up |
| +24h, automatic | Ranked; report lands in `reports/` and on Telegram |
| You | Tap **Share with everyone** on the winner (and the runner-up, for an A/B test) |
| You | Actions -> **Graduate and promote** -> paste the batch id |
| 10:00 TR, automatic | AI reviews the ad account and reports proposals |

The ad is created **paused**, and API-created campaigns appear in Ads Manager as
unpublished drafts behind **Review and Publish**. Nothing spends without you.

Commands, if you prefer the CLI:

```bash
python -m reelforge check                 # verify everything is wired up
python -m reelforge publish               # render + publish a batch
python -m reelforge measure --force       # rank now, ignoring the age threshold
python -m reelforge status                # what is in flight
python -m reelforge graduate <id> --also runner_up_key   # record what you tapped
python -m reelforge promote <id> --cells 2               # paused A/B test
python -m reelforge ads                                  # AI account review
python -m reelforge ads --apply                          # execute what passes
```

### Getting an A/B test rather than a single ad

`promote` builds a real split test when **two or more graduated variants** are
available — one ad set per contender under a single campaign, so Meta divides
the audience cleanly. Graduate the runner-up too and record it:

```bash
python -m reelforge graduate 20260725-0900-ab12ef --also hook_trim
```

With only one graduated variant it falls back to a single ad and says so.
Each cell carries its own budget, so two cells at 150 TRY/day is 300 TRY/day
once you activate them.

---

## Music: read this before adding tracks

This pipeline ends in an ad, and that changes the rules. Meta's Content Rights
Management system checks the audio on anything you boost. Unlicensed music
means **the ad is rejected and a copyright flag lands on the account — three
flags in 90 days restricts audio on all of your ads.**

Instagram's in-app music library does **not** grant commercial rights, and
business accounts are limited to the Meta Sound Collection anyway.

So the pipeline never fetches music. It reads whatever you put in
`/ReelForge/Muzik`, and the AI picks which track suits which treatment.

**Free and cleared for ads:** Meta Sound Collection (~15,000 tracks) in
Meta Business Suite. Download the ones you like and drop them in the folder.
Paid alternatives with ad licences: Epidemic Sound, Artlist.

Leave the folder empty and every treatment simply renders without a bed.
Nothing breaks.

## Generative video (optional)

`FAL_KEY` enables [fal.ai](https://fal.ai/docs/documentation) for genuinely
generated visuals rather than filtered ones.

Higgsfield was the original request. Its REST API is gated behind higher tiers
and the contract is not publicly documented, so it cannot be driven unattended
— fal.ai hosts the same underlying models (Veo 3, Seedance, Kling, Grok
Imagine) behind a documented queue API and pay-per-use billing.

Without the key nothing generative runs and the deterministic treatments carry
the whole batch.

## The ten treatments

A treatment is a complete creative take, not a single lever. Each one stacks:

| Layer | Options |
|---|---|
| **Motion** | head trim, speed, push-in, tighter crop, freeze on the opening frame — combinable |
| **Grade** | brightness, contrast, saturation, gamma, temperature |
| **Hook** | copy, one of 5 bundled fonts, 5 styles (`solid_bar`, `boxed`, `outline`, `underline`, `shadow_only`), 10 palette colours, 4 animations, 4 positions |
| **Music** | which licensed bed, gain, start offset, whether to duck the original audio |
| **Finish** | vignette on or off |

That is what makes ten renders look like ten different edits rather than ten
exports of one. The AI composes all of it per clip; without a key the built-in
catalogue does the same thing with fixed combinations.

A `control` baseline is always present, injected if the model omits it —
without it you cannot tell whether an edit helped or merely differed. All
treatments carry the **same caption**, so the edit stays the only variable.

Fonts live in `assets/fonts` (SIL OFL). Hook text is capped at 42 characters,
stripped of emoji and hashtags, and upper-cased, because `drawtext` cannot
render emoji from these faces and long lines wrap badly on a phone.

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
caps in `src/reelforge/treatment.py` only after checking a render against
the real garment.

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

157 tests. The editor ones render real media through ffmpeg, because filtergraph
mistakes otherwise surface at 06:00 UTC rather than in CI. The AI ones feed
malformed and hostile model output through the validators — invented ad set
ids, out-of-range saturation, emoji in hook text — because that is exactly what
a model will eventually return.
