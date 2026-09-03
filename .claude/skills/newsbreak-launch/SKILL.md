---
name: newsbreak-launch
description: TRIGGER "launch newsbreak <ACCOUNT>" (e.g. "launch newsbreak RF") -- resolves to `python scripts/launch_ads.py --account <ACCOUNT> --images-dir meta-ads-automation/images/<ACCOUNT>/_launched --videos-dir meta-ads-automation/videos/<ACCOUNT>/_launched --vertical <ACCOUNT's vertical> --execute` (confirm the dry-run output with the user before adding --execute). Creates new NewsBreak native ads (image or video) in an account's existing ad set by cloning an already-ACTIVE ad's non-creative settings (brandName, logoUrl, clickThroughUrl, callToAction) as a template per media type, swapping in a new image/video asset, and generating fresh Title (<=90 chars) / Description (<=90 chars) ad copy via Gemini -- both fields required to include the literal "{city}" token (NewsBreak's own serve-time city-insertion macro, never paraphrased). For a file that was already launched to Meta first (via meta-launch-tier1/meta-launch-direct, which always saves a transcript to video-analysis's winners/<stem>.md), copy generation reuses that existing transcript with a cheap text-only Gemini call instead of re-uploading and re-analyzing the raw media -- falls back to a fresh video/image upload+analysis only when no local transcript exists for that exact file. The destination clickThroughUrl gets a `dh1=<slug>` tracking param appended (matching meta-ads-automation's own convention) -- the slug is a separate 8-10 word title-cased phrase Gemini generates specifically for this, not the display title/description (which carry the {city} macro and shouldn't be dropped into a URL). SHA256 file-hash dedup against launched_registry/<ad_account_id>.json (one shared registry per account, not per ad set) means a folder can be safely re-run -- already-launched files are skipped, not relaunched, no matter which of the account's ad sets they landed in. NewsBreak caps an ad set at 50 ads (confirmed live via error code 41108) -- launch_ads.py now handles this automatically mid-batch: it tracks the live ad count and, on approaching the cap, creates a new ad set cloned from the current one's settings and keeps going into it, recording the new ad set in accounts.json's adset_ids list (a list now, not a single adset_id) when --account was used. Also retries automatically on an undocumented NewsBreak write-throttle (a generic "Invalid operation" message under sustained heavy load) with a real backoff. Accepts --account <key> (resolves ad-account-id/ad-set-id from accounts.json) or raw --ad-account-id/--ad-set-id, plus --template-from-account <key> to bootstrap a template from another ad set (same account: reuses its real clickThroughUrl as-is; different account: resolves clickThroughUrl from this ad set's own trackingId's Event instead), plus --test [N] to preview/launch just the first N files. Use this whenever the user wants to add new image or video ad creative to an existing NewsBreak ad set/campaign that's already running. This is a WRITE skill -- dry-run by default (no uploads, no ad creation, but the real Gemini copy-generation calls happen so the preview is accurate), only makes real changes with --execute. Manual trigger only.
---

# NewsBreak Launch

Creates new NewsBreak ads (image or video) in an existing, already-running
ad set by cloning a currently-ACTIVE ad of the matching media type as the
template for everything except the creative itself.

## What gets cloned vs. generated fresh

**Cloned verbatim from the matching-type ACTIVE template ad** (found via
`find_template_ad()`): `brandName`, `logoUrl`, `callToAction`, and the base
`clickThroughUrl` (before the `dh1` param gets appended -- see below).

**Generated fresh per media file:**
- The actual image/video asset itself, uploaded via `/ad/uploadAssets`.
- Title (<=90 chars) and Description (<=90 chars), via Gemini
  (`src/custom_copy.py`) -- both required to include the literal `{city}`
  token verbatim (NewsBreak's own ad-server macro, substituted with the
  viewer's real city at serve time; never paraphrase or translate it).

  **Transcript reuse (added 2026-09-02):** `_local_transcript()` checks
  `video-analysis/winners/<file's own stem>.md` first -- if this exact
  file was already launched to Meta via `meta-launch-tier1`/
  `meta-launch-direct` (both always generate that report before
  launching), its `### FULL TRANSCRIPT` section is reused directly and
  copy is generated with a single cheap text-only Gemini call
  (`analyze_text()`), no video/image upload or multimodal analysis at all.
  Prints `(reusing local transcript from winners/<stem>.md -- no
  video/image upload needed)` when this happens. Only falls back to the
  original upload-and-analyze flow (`analyze_video()`/`analyze_image()`)
  when no matching report exists -- e.g. creative that's NewsBreak-only
  and was never launched to Meta first. Since this project's real batches
  so far have all sourced media from `meta-ads-automation`'s own
  `_launched/` folders (see below), the cache hit is the common case, not
  the exception.
- A `url_slug_title` -- a separate 8-10 word, title-cased phrase Gemini
  writes specifically for the URL tracking slug (not the same text as the
  title/description, since those carry the `{city}` macro which has no
  business in a URL param). `custom_copy.slugify()` turns it into
  `dh1=Word+Word+Word...`, appended to the template's `clickThroughUrl`
  (`?dh1=` if the URL has no query string yet, `&dh1=` if it does) --
  same convention as `meta-ads-automation`'s own `build_custom_url()`.
- Ad name: `IMG <hash8>` or `VID <hash8>`, the first 8 hex chars of the
  file's own SHA256.
- Video cover thumbnail (video ads only): first frame via ffmpeg.

## Manual single-ad bootstrap (e.g. seeding a brand-new account's first ad)

When there's no existing ad at all to clone from (a fresh account/ad set,
before `launch_ads.py`'s normal template-cloning flow has anything to work
with -- see `clone_structure.py` for the campaign/ad-set side of this same
bootstrap) and the user is supplying their own `clickThroughUrl` and
creative directly rather than running a folder batch, still use
`custom_copy.generate_ad_copy()` for the Title/Description (not
hand-written copy) and still append `dh1` to the given URL the same way
`build_click_through_url()` does for a normal batch run -- confirmed
explicitly, 2026-09-03 (Bathroom's first ad): a manually-built ad
shouldn't skip either convention just because it's outside the normal
`launch_ads.py` loop.

```python
import sys
sys.path.insert(0, 'src')
import custom_copy

copy = custom_copy.generate_ad_copy(media_path, vertical=..., brand_name=..., is_video=...)
slug = custom_copy.slugify(copy['url_slug_title'])
base_url = "<the user-supplied clickThroughUrl>"
separator = "&" if "?" in base_url else "?"
click_through_url = f"{base_url}{separator}dh1={slug}"
```

`copy['title']`/`copy['description']` go straight into the ad creative's
`headline`/`description` (both already `{city}`-validated and length-capped
by `generate_ad_copy()` itself) -- `click_through_url` above is what goes
into `creative['clickThroughUrl']`, never the user's raw URL unmodified.
brandName/logoUrl/callToAction still get cloned from a proven template ad
(another account's, re-uploading its logo image as a fresh asset for the
new account -- see `clone_structure.py`'s own logo-recopy logic), same as
every other bootstrap in this project. Register the result in
`launched_registry/<ad_account_id>.json` by the media file's own SHA256
same as a normal launch, so a later `launch_ads.py` batch run over the
same folder correctly skips it instead of relaunching it.

## Dedup

`launched_registry/<ad_account_id>.json` (keyed by ad account, not ad
set, since 2026-09-03 -- see "Ad set overflow" below for why) maps each
file's full SHA256 hash to the ad ID it produced. A file already in the
registry is skipped on any later run -- safe to re-run the same folder
repeatedly (e.g. resuming an interrupted batch, or re-running after
adding new files to the same folder) without relaunching anything already
live, and without caring which of the account's (possibly several) ad
sets it ended up in.

## Ad set overflow: NewsBreak's 50-ads-per-ad-set cap -- now AUTOMATIC (2026-09-03)

Confirmed live (RF, then HVAC): NewsBreak rejects ad creation with
`NewsBreak API error 41108: Exceed 50 ads limit under one ad set!` once an
ad set's own ad count reaches roughly that cap (both accounts got real ads
through up to 57/59 total before the error actually fired -- the cap isn't
enforced exactly at 50, so don't rely on that slack). There is no
larger-ad-set option -- the fix is always a new ad set under the same
campaign, cloned from the current one's exact settings.

**This used to be a manual multi-step procedure** (create the ad set by
hand, copy the dedup registry file, register it in `accounts.json`,
restart the batch with `--template-from-account`) -- repeating it by hand
for both RF and HVAC on the same day was the direct trigger for
automating it. As of 2026-09-03, `launch_ads.py` handles all of this
itself, mid-batch, with no manual intervention:

- **Live ad-count tracking**: fetched once at startup for whichever ad set
  is about to be filled, then updated in-memory after every real success
  (no extra API call per video). Once the count reaches `AD_SET_ROTATE_AT`
  (45 -- comfortably under the ~50-57 cap window) the very next ad
  triggers `create_overflow_adset()`: it reads the current ad set's exact
  live settings (`api.get_ad_sets()`), names the new one by incrementing
  the "Ad Set N" pattern in the current name (`next_adset_name()` --
  falls back to appending "(overflow)" if a name doesn't match that
  pattern), and clones budget/bidType/trackingId/targeting/platforms/
  frequencyCaps/status via `api.create_ad_set()`. The batch then keeps
  going into the new ad set without missing a beat -- only `--execute`
  mode tracks/rotates at all; a dry run never needs to (nothing is
  actually being created).
- **`accounts.json` schema** (migrated 2026-09-03): `adset_id`/`adset2_id`
  singular fields are gone, replaced by **`adset_ids`: a list**, in
  creation order. `config.current_adset_id(key)` (the last entry -- the
  one currently being filled) resolves `--account`'s target ad set;
  `config.get_adset_ids(key)` returns the whole list; `config.append_adset_id(key, new_id)`
  is what `create_overflow_adset()` calls to record a new one -- only when
  `--account` was actually given (a raw `--ad-account-id`/`--ad-set-id`
  invocation still auto-creates the overflow ad set, just prints its ID
  instead of persisting it anywhere, since there's no account key to
  write against).
- **Dedup registry keyed by `ad_account_id`, not `ad_set_id`** (migrated
  2026-09-03, old per-ad-set files left in place under
  `launched_registry/`, just unused going forward) -- one shared registry
  per account means rotating ad sets never needs a registry copy/reseed
  step at all; a file already launched into an earlier ad set is
  recognized as already-launched no matter which ad set is currently being
  filled.
- **Template lookup spans every ad set the account has ever had**
  (`find_foreign_template()`/`get_template()`'s local search both now
  query `config.get_adset_ids(key)` in full, not just the current one) --
  an account's only ACTIVE template ad of a given creative type may sit in
  an earlier ad set once a later one is a freshly-created, still-empty
  overflow.

Nothing about launching a batch changes from the user's side -- run it the
same way regardless of whether the account has one ad set or five; rotation
just happens silently in the output (`Ad set <id> is nearing NewsBreak's
50-ad cap -- creating overflow ad set '<name>' cloned from its settings...`)
when/if it's needed.

**Same-account clickThroughUrl fix (2026-09-02):** when `--template-from-account`
resolves to the SAME ad account as the one being launched into,
`get_template()` now reuses the foreign template ad's own real
`clickThroughUrl` as-is, instead of resolving one from the ad set's own
trackingId's Event (the cross-account bootstrap path -- see the HVAC
example below). Confirmed live this matters: RF's own trackingId's Event
had its `url` field registered as a completely different domain
(`home-improvements.co`, the POSTBACK callback endpoint) than what RF's
real ads actually use as `clickThroughUrl` (`home-improvements.work`, the
real user-facing landing page) -- these are NOT interchangeable. The
event-resolution fallback is still correct and still used for a genuinely
different account's ad set (e.g. bootstrapping a brand-new account like
HVAC that has no landing URL of its own yet -- see `clone_structure.py`'s
own history for that case).

## Running it

```bash
cd "C:\Users\USER\newsbreak-ads-automation"

# Dry run -- --account resolves ad-account-id/ad-set-id from accounts.json
python scripts/launch_ads.py --account RF \
    --images-dir "C:\Users\USER\meta-ads-automation\images\RF\_launched" \
    --videos-dir "C:\Users\USER\meta-ads-automation\videos\RF\_launched" \
    --vertical roofing --test 2

# Real run, once the dry run's generated copy looks right
... same args ... --execute

# Raw IDs still work for an account not yet in accounts.json
python scripts/launch_ads.py --ad-account-id <id> --ad-set-id <id> --images-dir ... --execute
```

`--images-dir` and/or `--videos-dir` -- at least one required. Every file
in the given folder(s) matching the known extensions gets processed
(images: `.png`/`.jpg`/`.jpeg`/`.webp`/`.gif`; videos: `.mp4`/`.mov`/
`.m4v`). `--test [N]` limits the run to the first N files (bare `--test`
defaults to 1) -- combine with `--execute` to launch just a few for real
before resuming the rest with a normal follow-up run (already-launched
files are skipped automatically via the dedup registry).

There's no per-account default source folder in this project the way
`meta-ads-automation` has `videos/<ACCOUNT>/_launched` as its own
convention -- so far, every real launch has sourced media straight from
`meta-ads-automation`'s own `images/<ACCOUNT>/_launched` and
`videos/<ACCOUNT>/_launched` folders (the same creative, launched to both
platforms). Point `--images-dir`/`--videos-dir` at wherever the actual
media for that account/vertical actually lives.

## accounts.json

`--account <key>` (e.g. `RF`, `HVAC`, `Nutra`) resolves `ad_account_id`
from `accounts.json`, and the currently-being-filled ad set from
`adset_ids` (a list, in creation order -- `config.current_adset_id(key)`
returns its last entry) -- see `src/config.py`. An account entry needs at
least one entry in `adset_ids` for this to work; `--ad-set-id` on the
command line overrides it either way. See `clone_structure.py` /
`newsbreak-ad-account-naming` project memory for how new accounts get
seeded with a working campaign/ad set structure in the first place, and
"Ad set overflow" above for how later entries get appended to `adset_ids`
automatically.

## Safety model

Dry-run by default -- prints the exact generated Title/Description/
clickThroughUrl and what ad it would create, without uploading anything
or creating real ads (the real Gemini analysis calls do happen in dry-run,
so the preview is the actual copy, not a placeholder). Only makes real
writes (asset upload, ad creation, registry write) with `--execute`.
Manual trigger only -- always show the dry-run output and get explicit
confirmation before adding `--execute`, same as every write script in
this project.
