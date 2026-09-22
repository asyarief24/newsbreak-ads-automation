#!/usr/bin/env python
"""Run this first, before trusting anything else in a new NewsBreak session.

Ported from meta-ads-automation's meta-session-check, same two-part shape,
but scoped to what this project actually has -- it has no Tier1/Campaign A
split and no S5 bid-strategy test campaigns, so those two checks don't
carry over. What DOES carry over, and what's real here:

**Structural drift checks** (these set the exit code):

  1. Is this local checkout in sync with origin? Same reasoning as the
     Meta version -- a checkout that's behind means every state file and
     log read below (and by any other script this session runs) may be
     stale. Confirmed live, 2026-09-22: this project's own checkout was
     found 24 commits behind origin during the exact investigation that
     led to building this check.
  2. Is each account's currently-filled ad set at or over NewsBreak's real
     50-ads-per-ad-set cap (AD_SET_CAP, confirmed live via error code
     41108 -- see launch_ads.py)? create_overflow_adset() auto-creates a
     fresh ad set once this is hit during a real --execute launch, but a
     dry-run-only batch, or a failed overflow creation, could leave an ad
     set silently sitting at/over cap with new ads unable to launch into
     it. This is this project's real structural-cap analogue to Meta's
     Tier 1 active-ad cap check.

No third check exists here the way Meta's Campaign A/S5 sync did --
that one existed because Campaign A and S5 are two *related* automations
that could desync from each other. NewsBreak has no equivalent pair (one
campaign, two ad sets, no separate test tier), so there's no comparable
invariant to assert. Manufacturing a check just for parity would be
worse than having only two.

**Situational awareness** (informational only, never affects the exit
code) -- today's real events from newsbreak-campaign-safeguard's and
newsbreak-ad-hogging-safeguard's own daily logs, so a session starts
knowing what the automation already did today. Both logs here are flat,
one-line-per-event text (`- HH:MM:SS UTC [ACCOUNT] ...`), not the
Markdown-block format meta-ads-automation's logs use -- every line is
already a real, actionable event (no --verbose-log noise to filter out
here, unlike the Meta side).

Deliberately self-contained, same reasoning as the Meta version: fast,
cheap, no dependency on any other skill. Read-only throughout -- never
writes to NewsBreak or to any state file, no --execute flag exists.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, newsbreak_api as api  # noqa: E402

# Confirmed live via NewsBreak's own error code 41108 -- see
# scripts/launch_ads.py's own AD_SET_CAP constant (duplicated here rather
# than imported, matching this project's convention of independent,
# non-coupled skill scripts).
AD_SET_CAP = 50

CAMPAIGN_SAFEGUARD_LOG_DIR = PROJECT_ROOT / "logs" / "campaign-safeguards"
AD_HOGGING_LOG_DIR = PROJECT_ROOT / "logs" / "ad-safeguards"


def check_git_sync() -> bool:
    print("=== Git sync ===")
    behind = config.git_commits_behind()
    if behind is None:
        print("  Could not check (no network, or not a git repo) -- proceeding anyway.")
        return True
    if behind == 0:
        print("  In sync with origin.")
        return True
    print(f"  BEHIND by {behind} commit(s) -- run 'git pull --rebase' before trusting "
          f"anything below, or anything else you run this session.")
    return False


def check_adset_cap(name: str, account: dict) -> bool:
    adset_ids = account.get("adset_ids") or []
    if not adset_ids:
        print(f"  {name}: no adset_ids configured -- skipping.")
        return True
    current_id = adset_ids[-1]
    try:
        ads = api.get_ads(
            account["ad_account_id"], ad_set_ids=[current_id], page_size=500,
        ).get("list", [])
    except Exception as e:
        print(f"  {name}: ERROR fetching ads for ad set {current_id} -- {e}")
        return False
    count = len(ads)
    if count >= AD_SET_CAP:
        print(f"  {name}: current ad set {current_id} has {count} ads >= {AD_SET_CAP} cap -- "
              f"AT/OVER CAP. Check launch_ads.py's create_overflow_adset() ran correctly, or "
              f"run a launch batch with --execute so it self-heals by creating an overflow ad set.")
        return False
    print(f"  {name}: current ad set {current_id} -- {count}/{AD_SET_CAP} ads.")
    return True


def print_log_summary(log_dir: Path, label: str, account_names) -> None:
    """Prints today's real events per account from a given daily log file.

    Both newsbreak-campaign-safeguard's and newsbreak-ad-hogging-safeguard's
    logs are flat `- HH:MM:SS UTC [ACCOUNT] ...` lines, one per real event --
    no verbose/noise mode exists on this project's safeguards the way
    meta-campaign-safeguard's --verbose-log does, so every line here is
    already worth showing, unlike the Meta version's noise-filtering.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    path = log_dir / f"{today}.md"
    print(f"\n=== {label} (today, {today}) ===")
    if not path.is_file():
        print("  No log file yet for today.")
        return

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    any_events = False
    for name in account_names:
        acct_lines = [line for line in lines if f"[{name}]" in line]
        if not acct_lines:
            continue
        any_events = True
        print(f"  --- {name} ---")
        for line in acct_lines:
            print(f"    {line}")
    if not any_events:
        print("  No real events today for any account.")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    all_ok = check_git_sync()

    accounts = config.load_accounts()

    print("\n=== Ad set cap (current ad set per account) ===")
    for name, account in accounts.items():
        all_ok = check_adset_cap(name, account) and all_ok

    print(f"\n{'=' * 70}")
    print("All checks passed." if all_ok else "Some checks FAILED -- see above.")

    # Everything below is informational context, not a pass/fail check --
    # the exit code above already reflects only the two structural drift
    # checks (git sync, ad set cap).
    print_log_summary(CAMPAIGN_SAFEGUARD_LOG_DIR, "campaign_safeguard", accounts)
    print_log_summary(AD_HOGGING_LOG_DIR, "ad_hogging_safeguard", accounts)

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
