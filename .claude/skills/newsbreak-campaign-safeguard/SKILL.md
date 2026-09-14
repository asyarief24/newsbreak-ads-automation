---
name: newsbreak-campaign-safeguard
description: Runs on a schedule across a named NewsBreak account's single campaign (both ad sets together -- this project has no Tier1/Campaign A split), watching for things NewsBreak's own delivery algorithm has no visibility into -- a dead landing page, a broken lead form, or a downstream buyer network that's stopped purchasing leads. Ported from meta-ads-automation's meta-campaign-safeguard (same 4-layer design: ROAS-vs-spend throttle, click->LPV crash, LPV->lead crash, lead->purchase crash), with thresholds re-derived from NewsBreak's own real trailing data rather than copying Meta's dollar figures -- this project's accounts run 10-50x smaller daily spend. This is a WRITE skill (pauses/resumes the campaign) -- dry-run by default, only makes real changes with --execute. LIVE as of 2026-09-15 (see Status below) -- the scheduled GitHub Actions workflow runs with --execute.
---

# NewsBreak Campaign Safeguard

Sibling skill to `newsbreak-ad-hogging-safeguard` (same tagging/logging
conventions, same dry-run-first rollout discipline). Full threshold
derivation lives in `campaign_safeguard.py`'s own module docstring --
read that first if a number here looks unexplained.

## What this is NOT

Not a performance judgment system -- this project has no equivalent yet of
meta-ads-automation's `meta-graduate-check` (trailing-window KEEP/KILL
per creative). This skill only answers a narrower question: is something
*technically or structurally broken* right now, regardless of how good the
creative is.

## Why this differs from meta-campaign-safeguard structurally

- **No Tier1/Campaign A split.** Each account here has exactly one campaign
  and two ad sets (see `accounts.json`). The whole campaign is the
  throttle/pause unit (both ad sets pause/resume together) -- confirmed as
  the right scope with the user 2026-09-14, since there's no separate
  "test" vs "scaled" campaign tier here yet the way Meta's Tier 1 vs
  Campaign A works.
- **No per-account timezone lookup exists in this project** (unlike Meta's
  `get_account_timezone`). This script runs entirely in UTC for both
  reporting windows and the midnight reset. Revisit if a real need for
  local-time resets shows up.
- **Event types aren't uniform across accounts.** RF has no lead-type event
  configured at all (only `complete_payment` + `view_content`) -- layers 3
  and 4 are skipped entirely for any account with an empty
  `lead_event_types` in `accounts.json`.

## The four layers, briefly

See `campaign_safeguard.py`'s docstring for the full real-data table this
was calibrated from (pulled live 2026-09-14).

1. **ROAS-vs-spend throttle** -- pauses if today's spend clears a floor and
   today's ROAS is below `ROAS_THROTTLE_FLOOR` (0.5, deliberately well below
   breakeven since 4 of 5 accounts already run underwater on trailing ROAS
   -- this is for acute crashes, not ordinary underperformance).
2. **Click -> LPV crash** -- catches a dead landing page. Compared against
   each account's OWN trailing 7-day baseline (healthy accounts here run
   94-112% click->LPV; there's no single universal "normal" number).
3. **LPV -> Lead crash** -- catches a broken lead form specifically. Skipped
   for accounts with no lead event configured.
4. **Lead -> Purchase crash** -- demand-side signal (buyer network stopped
   purchasing). Skipped for accounts with no lead event configured.

## Running it

```bash
cd "C:\Users\USER\newsbreak-ads-automation"

# Dry run -- always do this first
python .claude/skills/newsbreak-campaign-safeguard/scripts/campaign_safeguard.py RF

# All accounts with a campaign_id configured
python .claude/skills/newsbreak-campaign-safeguard/scripts/campaign_safeguard.py all

# Real run
python .claude/skills/newsbreak-campaign-safeguard/scripts/campaign_safeguard.py RF --execute

# Log every check's result, not just actionable events (useful during burn-in)
python .claude/skills/newsbreak-campaign-safeguard/scripts/campaign_safeguard.py all --verbose-log
```

Scheduled via `.github/workflows/campaign-safeguard.yml`, every 30 minutes
(same cadence as Meta's, GitHub's own trigger is best-effort with no SLA --
design around arbitrarily large gaps between runs).

## State and logging

- `campaign_safeguard_state/<ACCOUNT>.json` -- `incidents`, keyed by layer
  tag (`roas_spend_throttle`, `lpv_rate_crash`, `lead_rate_crash`,
  `purchase_rate_crash`), tracking the currently-paused incident (if any)
  for that layer: when it tripped, how many real retests it's used today.
  Only written in `--execute` mode.
- `logs/campaign-safeguards/<UTC date>.md` -- one file per day, appended to
  when a pause/resume/retest actually happens (or would have, in dry-run).
  `--verbose-log` also logs every healthy check.

## No notification

Fully autonomous by design, matching the Meta-side safeguards -- no Slack,
email, or GitHub Issue. The daily log is the only visibility.

## Status: LIVE (as of 2026-09-15)

Burn-in reviewed directly (one full day, 2026-09-14, across all 5
accounts). Real finding: every Layer 1 (ROAS-vs-spend) trip that day showed
ROAS exactly 0.00 -- zero purchases, not just underwater -- the same
attribution-lag false-positive `meta-ad-hogging-safeguard` had already
found and fixed on the Meta side. Fixed here with
`ROAS_THROTTLE_ZERO_PURCHASE_MULTIPLIER` (doubles the spend floor when
today has zero purchases) and re-verified live immediately after: RF
correctly read healthy once a real purchase landed (ROAS 1.48), Flooring
correctly held off at $31.61/0 purchases (below the doubled bar), while
Bathroom ($46.78/0 purchases) and Siding ($41.46/0 purchases) still
correctly tripped -- both had genuinely cleared even the doubled bar, a
real high-confidence signal. `.github/workflows/campaign-safeguard.yml`
now runs with `--execute`; `--verbose-log` dropped (was only on for
burn-in review).

**Thresholds are still a first pass**, not tuned over weeks the way Meta's
were -- see the module docstring's derivation table. Revisit as more real
trailing data accumulates, especially once accounts run longer than the
current ~2 weeks of history.

## If something looks wrong

- **The campaign is paused and you don't know why** -- check
  `campaign_safeguard_state/<ACCOUNT>.json` for the tripped layer's tag, or
  the day's log file for the exact numbers.
- **It won't resume** -- check `retests` in the state file against
  `MAX_REAL_RETESTS_PER_DAY` (2); if exhausted, it's intentionally waiting
  for the 00:00 UTC backstop.
- **HVAC acting oddly** -- its campaign was already manually OFF as of
  2026-09-14, unrelated to this script; a campaign that's already OFF for
  other reasons just won't show any of this script's own pause reasons.
