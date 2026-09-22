---
name: newsbreak-session-check
description: Run this first, before trusting anything else in a NewsBreak session. Two structural drift checks that set the exit code -- is this local checkout in sync with origin, and is each account's currently-filled ad set at or over NewsBreak's real 50-ads-per-ad-set cap (confirmed via error code 41108) -- plus an informational section reporting today's real events from newsbreak-campaign-safeguard's and newsbreak-ad-hogging-safeguard's own daily logs. Ported from meta-ads-automation's meta-session-check, scoped down to what this project actually has (no Tier1/Campaign A split, no S5 test campaigns). Manual trigger only -- not part of any scheduled/automated flow the way it's meant to be used by a human/Claude session, though a GitHub Actions cron wrapper also runs it on a schedule for drift detection outside of active sessions. Trigger this whenever starting work on the newsbreak-ads-automation project, or when the user says "newsbreak session check" or asks to check NewsBreak's automation health/drift.
---

# NewsBreak Session Check

Sibling to `meta-ads-automation`'s `meta-session-check` -- same two-part
shape (structural checks that set the exit code, then informational
situational awareness), same fast/cheap/self-contained design so it's
worth running often. Ported 2026-09-22, at the user's request, after a
live investigation found this project's own local checkout 24 commits
behind origin -- the exact class of problem meta-session-check's git
check already exists to catch.

## Why this isn't a line-for-line port

Meta's three structural checks were built from three specific real
incidents. Two of them don't have a NewsBreak equivalent:

- **Tier 1 active-ad cap** -- this project has no Tier1/Campaign A split
  at all (one campaign, two ad sets per account, see
  `newsbreak-campaign-safeguard`'s own SKILL.md). What NewsBreak DOES have
  that's structurally analogous is a real, hard **50-ads-per-ad-set cap**
  (confirmed live via error code 41108 -- see `scripts/launch_ads.py`'s
  `AD_SET_CAP` and `create_overflow_adset()`). `launch_ads.py` auto-creates
  an overflow ad set once it hits this during a real `--execute` launch,
  but a dry-run-only batch, or a failed overflow creation, could leave an
  ad set silently sitting at/over cap -- this check catches that.
- **Campaign A / S5 sync** -- this existed because Campaign A and S5 are
  two *related* automations that could desync from each other. NewsBreak
  has no equivalent pair here (no separate test tier), so there's no
  comparable invariant to assert. Manufacturing a third check just for
  parity with the Meta version would be worse than having only two real
  ones -- see meta-session-check's own SKILL.md for the exact same
  reasoning it applied when S5 sync was later retired there.

## Running it

```bash
cd "C:\Users\USER\newsbreak-ads-automation"
python .claude/skills/newsbreak-session-check/scripts/session_check.py
```

No `--execute` flag exists -- read-only throughout, never writes to
NewsBreak or to any state file. Exits non-zero if either structural check
fails.

## The two structural checks (set the exit code)

1. **Git sync** -- reuses `config.git_commits_behind()` (added to this
   project's `src/config.py` alongside this skill, same signature as
   `meta-ads-automation`'s own). A checkout that's behind means every
   state file and log read below -- and every other script's state this
   session might touch -- could be stale.
2. **Ad set cap** -- for each account with `adset_ids` configured, counts
   real ads in the *currently-filled* ad set (the last one in the list --
   see `config.current_adset_id()`) via a live `get_ads` call, and flags
   it if at or over NewsBreak's 50-ad cap. Skipped for accounts with no
   `adset_ids` yet (e.g. `Nutra`).

## Situational awareness (informational only, never affects the exit code)

Printed after the two checks above, reading straight from this project's
own daily logs:

3. **`newsbreak-campaign-safeguard` today, per account** -- reads
   `logs/campaign-safeguards/<today>.md`.
4. **`newsbreak-ad-hogging-safeguard` today, per account** -- reads
   `logs/ad-safeguards/<today>.md`.

Both logs here are flat, one-line-per-event text
(`- HH:MM:SS UTC [ACCOUNT] ...`), not the Markdown-block format
`meta-ads-automation`'s logs use -- every line is already a real,
actionable event (this project's safeguards have no `--verbose-log`
noise-mode the way the Meta side does), so there's no noise-filtering
regex here, just a plain substring match on `[ACCOUNT]`.

## What to do when a check fails

- **Git behind**: `git pull --rebase`, then re-run this check.
- **Ad set over cap**: run a real launch batch with `--execute` for that
  account (`python .claude/skills/newsbreak-launch/scripts/...` -- see
  `newsbreak-launch`'s own SKILL.md) so `create_overflow_adset()` fires
  and self-heals it, or investigate why overflow creation didn't already
  fire on a prior launch.

## Automation

Also wired into `.github/workflows/session-check.yml`, scheduled every 6
hours (matching the equivalent wrapper added to `meta-ads-automation` the
same day, at the user's explicit request) -- uses the existing
`NEWSBREAK_ACCESS_TOKEN` repo secret, same credential path
`campaign-safeguard.yml`/`ad-hogging-safeguard.yml` already use. Read-only,
so a clean run is silent (green); a real drift/cap failure shows red and
triggers GitHub's default failure-notification email. This deviates from
the "manual trigger only" framing above -- that framing describes the
check's original design intent (a preflight for a human/Claude session
starting work), not a restriction against also running it unattended for
drift detection between sessions.

## What this deliberately does not do

- Doesn't fix anything itself -- both failures print the exact follow-up
  action, but this script never calls `--execute` on your behalf.
- Doesn't run `newsbreak-campaign-safeguard`, `newsbreak-ad-hogging-safeguard`,
  or any other skill -- self-contained by design, same reasoning as
  `meta-session-check`.
