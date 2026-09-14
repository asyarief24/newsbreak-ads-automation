---
name: newsbreak-ad-hogging-safeguard
description: Runs on a schedule across a named NewsBreak account's two ad sets, pausing any single ad taking more than its fair share of that ad set's own daily budget while performing badly today -- so other ads sharing the same budget get a fair chance. Separate concern from newsbreak-campaign-safeguard's whole-campaign ROAS-vs-spend throttle -- this is ad-level and mostly reversible same-day, plus a separate permanent lifetime-backstop kill for ads that have proven themselves genuine duds. Ported from meta-ads-automation's meta-ad-hogging-safeguard (same same-day throttle + lifetime-backstop design), with thresholds re-derived from NewsBreak's own real trailing data -- fair share here is $0.47-$0.93/ad/day, roughly 50-100x smaller than Meta's Campaign A. This is a WRITE skill (pauses/resumes individual ads) -- dry-run by default, only makes real changes with --execute. LIVE as of 2026-09-15 (see Status below) -- the scheduled GitHub Actions workflow runs with --execute.
---

# NewsBreak Ad Hogging Safeguard

Sibling skill to `newsbreak-campaign-safeguard` (same tagging/logging
conventions, same dry-run-first rollout discipline). Full threshold
derivation and the real per-ad-set fair-share table live in
`ad_hogging_safeguard.py`'s own module docstring.

## What this is NOT

Not a performance judgment system, and not the same as
`newsbreak-campaign-safeguard`'s whole-campaign ROAS-vs-spend throttle.
This answers a narrower, ad-level question: is one ad burning a
disproportionate share of its ad set's budget today while doing badly? And
separately: has an ad proven itself a genuine dud over its whole recent
history regardless of any one day's numbers?

## Why this differs from meta-ad-hogging-safeguard structurally

- **Fair share is computed per ad set, not per campaign.** Unlike Meta's
  Campaign A (one shared-pool ad set with the budget), each account here
  has TWO ad sets, each with its own daily budget (NewsBreak puts budget on
  the ad set, not the campaign -- confirmed live, `campaign.budget` is
  always `None` here). Every check runs independently per ad set.
- **Much smaller dollar scale.** Fair share here is $0.47-$0.93/ad/day
  (budget / active ad count, 25-58 active ads per ad set) vs. Meta's
  $40-50/ad/day. All dollar thresholds (`AD_HOG_MIN_SPEND_FLOOR_USD`,
  the three lifetime backstop figures) are new numbers for this scale, not
  Meta's $50/$100/$200/$100.
- **Individual ads are mostly evidence-starved same-day.** Account-wide
  leads are single digits/day and purchases are 0-4/day, split across
  25-58 ads per ad set -- most ads show 0 leads on any given day. The
  same-day throttle will rarely fire with real confidence; the **lifetime
  backstop is expected to be the primary real enforcement mechanism** here,
  more so than it already was for Meta's version. Confirmed live on first
  run, 2026-09-14: the lifetime backstop alone found 2-7 real dud/no-lead
  ads per account on the very first dry run.
- **"Lifetime" means a 30-day trailing window** (NewsBreak's report API
  caps useful lookback there), not literal all-time. These accounts are
  only ~2 weeks old as of this writing, so it's a reasonable proxy for now
  -- revisit once accounts are genuinely older than 30 days.
- **No per-account timezone lookup** -- runs entirely in UTC, same
  simplification as `newsbreak-campaign-safeguard`.

## Same-day throttle: the trip condition

```
PAUSE this ad if ALL of:
  - ad is currently ON
  - today's spend > max(5x this ad set's fair share, $3 floor)
  - underperforming, best evidence first:
      >= 2 purchases today -> ROAS < 1.0
      else >= 2 leads today AND pool has >= 5 leads today -> CPL > 1.5x pool CPL
      else 0 leads today -> that IS the verdict
      else (1 lead, no benchmark) -> no action
  - hasn't used its 2 pause/resume round trips today
```

Resume: passive, re-evaluated every run (ROAS/CPL recovers, or an
unconditional 00:00 UTC forced resume for anything neither path resolved).

## Lifetime backstop: a real, permanent kill

Any currently-ON ad tripping any of these (over the last 30 days):

1. **No-lead**: lifetime spend > $10, zero leads ever (skipped for an
   account with no `lead_event_types` configured, e.g. RF).
2. **Dud**: lifetime spend > $15, zero purchases ever.
3. **Bad ROAS**: lifetime spend > $25, lifetime ROAS < 0.5.

Tracked in `state["lifetime_kills"]`, separate from the same-day
`state["incidents"]`, so the midnight backstop (which unconditionally
reactivates same-day incidents) can never accidentally resurrect a
permanent kill. A separate same-day check re-runs these three conditions
for anything in `lifetime_kills` that shows real spend today, undoing the
kill if a late-attributing conversion means it no longer trips.

## Running it

```bash
cd "C:\Users\USER\newsbreak-ads-automation"

# Dry run -- always do this first
python .claude/skills/newsbreak-ad-hogging-safeguard/scripts/ad_hogging_safeguard.py RF

# All accounts
python .claude/skills/newsbreak-ad-hogging-safeguard/scripts/ad_hogging_safeguard.py all

# Real run
python .claude/skills/newsbreak-ad-hogging-safeguard/scripts/ad_hogging_safeguard.py RF --execute

# Log every check's result, not just actionable events (useful during burn-in)
python .claude/skills/newsbreak-ad-hogging-safeguard/scripts/ad_hogging_safeguard.py all --verbose-log
```

Scheduled via `.github/workflows/ad-hogging-safeguard.yml`, every 15
minutes (same cadence as Meta's; GitHub's trigger is best-effort, design
around gaps).

## State and logging

- `ad_safeguard_state/<ACCOUNT>.json` -- `incidents` (same-day throttle,
  keyed by ad ID) and `lifetime_kills` (permanent kills, keyed by ad ID),
  each entry tagged with which ad set it belongs to. Only written in
  `--execute` mode.
- `logs/ad-safeguards/<UTC date>.md` -- appended to only when a real
  pause/resume/kill happens (or would have, in dry-run). `--verbose-log`
  also logs every checked ad, including healthy ones.

## No notification

Fully autonomous, same as `newsbreak-campaign-safeguard` -- no Slack,
email, or GitHub Issue.

## Status: LIVE (as of 2026-09-15)

Burn-in reviewed directly (one full day, 2026-09-14, across all 5
accounts). The lifetime backstop found the same ~27 real dud/no-lead ads
on every single check throughout the day with identical numbers each time
(e.g. RF ad `2091751561375313921`: $60.28 lifetime spend, ROAS 0.20) --
completely stable, no flip-flopping. The same-day throttle also correctly
caught a genuinely worsening case (RF ad `2094290640131645442`: $4.26 ->
$6.84 -> $10.76 spent through the day, zero leads throughout). No false
positives observed. `.github/workflows/ad-hogging-safeguard.yml` now runs
with `--execute`; `--verbose-log` dropped (was only on for burn-in review).

**Thresholds are still a first pass** -- see the module docstring's
derivation table. In particular, the same-day throttle's fair-share
multiplier (5x) and CPL benchmark logic haven't yet had a chance to prove
themselves against a real trip the way Meta's went through several real
threshold revisions (`AD_HOG_FAIR_SHARE_MULTIPLIER` itself was redefined
twice on the Meta side before settling) -- expect this to need real-world
adjustment once it actually fires live.

## If something looks wrong

- **An ad is paused and you don't know why** -- check
  `ad_safeguard_state/<ACCOUNT>.json` for its tag (`incidents` for
  same-day, `lifetime_kills` for permanent), or the day's log file for the
  exact numbers.
- **Nothing ever trips the same-day throttle** -- expected at this scale
  most days; see "Individual ads are mostly evidence-starved same-day"
  above. The lifetime backstop is doing the real work here.
