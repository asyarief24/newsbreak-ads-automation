#!/usr/bin/env python
"""Runs on a schedule (see .github/workflows/campaign-safeguard.yml) across a
named account's single campaign (both ad sets together -- this project has
no Tier1/Campaign A split the way meta-ads-automation does), watching for
things NewsBreak's own delivery algorithm has no visibility into: a landing
page or lead form breaking, or a downstream buyer network no longer
purchasing leads. Ported from meta-ads-automation's meta-campaign-safeguard
(same 4-layer design, same tag/resume state machinery) -- adapted for a
materially different scale and data model, NOT a blind copy of its dollar
thresholds. See this file's own threshold comments below for the real
2026-09-14 trailing-data derivation of every number here.

Four independent layers, all sharing the same tag-and-resume state machinery
(campaign_safeguard_state/<ACCOUNT>.json):

  1. ROAS-vs-spend throttle -- pauses the campaign if today's spend has
     crossed ROAS_THROTTLE_MIN_SPEND_USD (doubled, ROAS_THROTTLE_ZERO_
     PURCHASE_MULTIPLIER, when today has zero purchases -- added 2026-09-15
     after the first day of burn-in showed every single trip that day was
     ROAS exactly 0.00, the same attribution-lag false-positive
     meta-ad-hogging-safeguard already found and fixed) and today's ROAS is
     below ROAS_THROTTLE_FLOOR. Re-evaluated every run even while paused (a
     late-attributing postback can flip it back to healthy on its own).

  2. Click -> LPV (view_content) crash -- catches a dead landing page.
     Gated on real click count (clicks keep accumulating regardless of what's
     broken downstream, so this is the one signal that can catch a TOTAL
     outage). Compared against this account's OWN trailing 7-day baseline
     rate rather than one fixed number, since healthy accounts here range
     94-112% while there is no reason to expect any account to sit at a
     "normal" universal figure.

  3. LPV -> Lead crash -- catches "page loads, form doesn't." Skipped
     entirely for an account with no lead_event_types configured (RF, as of
     2026-09-14 -- see accounts.json).

  4. Lead -> Purchase crash -- demand-side signal, not a technical outage.
     Also skipped for an account with no lead_event_types configured; falls
     back to nothing extra since layer 1 already covers the ROAS symptom of
     a dead buyer network for those accounts.

Recovery: capped real reactivate-and-retest cycles per UTC day
(MAX_REAL_RETESTS_PER_DAY), then an unconditional forced resume at 00:00 UTC
for anything that never recovered same-day. No per-account timezone lookup
exists in this project yet (unlike Meta's get_account_timezone) -- this
script runs entirely in UTC for both reporting and the midnight reset until
one is added.

Every pause/resume is tagged with which layer caused it, so the midnight
resume and retest logic only ever touch incidents this script created --
never a campaign paused manually.

No notification of any kind -- fully autonomous by design, matching the
Meta-side safeguards. The only visibility is logs/campaign-safeguards/
<UTC date>.md, written only when something actually happens.

Dry-run by default; --execute makes real writes.

--- Threshold derivation (real trailing 14-day data pulled 2026-09-14) ---

Account   | $/day (avg) | clicks/day | click->LPV | LPV->lead | lead->purch | ROAS
RF        | ~$90.66     | 18-123     | 94.3%      | 9.3%      | 27.4%       | 1.06
HVAC      | ~$23.74     | 5-45       | 31.1%      | 7.6%      | 0.0%        | 0.00
Bathroom  | ~$40.22     | 20-48      | 111.9%     | 18.7%     | 13.9%       | 0.59
Siding    | ~$36.92     | 8-35       | 105.2%     | 5.3%      | 21.4%       | 0.44
Flooring  | ~$34.14     | 16-92      | 94.5%      | 5.5%      | 31.2%       | 0.21

This is 10-50x smaller daily spend than meta-ads-automation's Campaign A
($100-600+/day there), so none of that project's dollar thresholds transfer
directly -- they were re-derived from the numbers above instead. HVAC's
31.1% click->LPV rate against everyone else's 94-112% is the clearest real
signal in this table -- a genuine, already-occurring case layer 2 exists to
catch (note: HVAC's campaign is currently manually OFF as of this writing,
unrelated to this script). Four of five accounts are already running
underwater on lifetime ROAS (0.21-0.59) -- ROAS_THROTTLE_FLOOR is set well
below breakeven specifically so this doesn't fire constantly on ordinary
underperformance that meta-graduate-check-style judgment (not yet built for
this project) should own, reserving this script for genuinely acute crashes.

These are explicitly a FIRST PASS, not burned in over weeks the way the
Meta-side numbers were -- revisit once more real data (and more accounts'
worth of it) comes in.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from src import config, newsbreak_api as nb  # noqa: E402

STATE_DIR = PROJECT_ROOT / "campaign_safeguard_state"
LOG_DIR = PROJECT_ROOT / "logs" / "campaign-safeguards"

# --- Thresholds (first pass -- see module docstring for derivation) ---

ROAS_THROTTLE_MIN_SPEND_USD = 20.0
ROAS_THROTTLE_FLOOR = 0.5
# Added 2026-09-15 after reviewing the first day of dry-run burn-in: every
# single Layer 1 trip that day (7 across RF/Siding/Bathroom/Flooring) showed
# ROAS EXACTLY 0.00 -- zero purchases recorded at all, not just a bad ratio.
# This is the identical false-positive pattern meta-ad-hogging-safeguard
# already found and fixed on the Meta side (see AD_MIN_PURCHASES_FOR_ROAS
# there): with zero purchases, "ROAS 0.00" is usually same-day attribution/
# postback lag, not a real crash, and is indistinguishable from a genuine
# dead buyer network without more evidence. Mirrors that same fix here --
# a zero-purchase reading needs to clear a higher spend bar before this
# layer trusts it, giving real attribution time to catch up; a reading with
# at least one real purchase is trusted at the normal floor.
ROAS_THROTTLE_ZERO_PURCHASE_MULTIPLIER = 2.0

LPV_CRASH_MIN_CLICKS = 15
LPV_CRASH_RATIO = 0.5  # trip below 50% of this account's own trailing baseline

LEAD_CRASH_MIN_LPV = 15
LEAD_CRASH_RATIO = 0.4

PURCHASE_CRASH_MIN_LEADS = 5
PURCHASE_CRASH_RATIO = 0.3

MAX_REAL_RETESTS_PER_DAY = 2
BASELINE_TRAILING_DAYS = 7
# Floor applied to a trailing baseline rate before using it as a comparison
# point -- protects against a thin/noisy trailing window producing a
# baseline so low that layers 2/3 could never trip (e.g. an account that
# itself had a bad trailing week). Deliberately conservative (lower than
# any single healthy account's real rate in the table above).
MIN_LPV_BASELINE_RATIO = 0.5
MIN_LEAD_BASELINE_RATIO = 0.03


def utc_today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_state(account_key: str) -> dict:
    path = STATE_DIR / f"{account_key}.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {"incidents": {}}


def save_state(account_key: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{account_key}.json").write_text(json.dumps(state, indent=2) + "\n")


def log_line(account_key: str, text: str, dry_run: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{utc_today_str()}.md"
    prefix = "[DRY RUN] " if dry_run else ""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- {ts} UTC [{account_key}] {prefix}{text}\n")


def event_counts(row: dict, event_types: list[str]) -> int:
    ec = row.get("eventCount", {}) or {}
    return sum(ec.get(et, 0) for et in event_types)


def fetch_window(account: dict, date_range: str) -> dict:
    """One AD_ACCOUNT-level row of {cost, click, lpv, leads, purchases,
    revenue} in dollars/counts (not cents), for the given NewsBreak
    dateRange enum (e.g. TODAY, LAST_7_DAYS)."""
    lpv_type = account.get("lpv_event_type")
    lead_types = account.get("lead_event_types", [])
    purchase_type = account.get("purchase_event_type")
    event_metrics = []
    for et in filter(None, [lpv_type, purchase_type, *lead_types]):
        event_metrics.append({"eventType": et, "metrics": ["COUNT", "VALUE"]})
    data = nb.get_report(
        account["ad_account_id"], dimensions=["AD_ACCOUNT"],
        metrics=["COST", "CLICK"], date_range=date_range,
        event_metrics=event_metrics or None,
    )
    rows = data.get("rows", [])
    row = rows[0] if rows else {}
    cost = row.get("cost", 0) / 100
    click = row.get("click", 0)
    lpv = event_counts(row, [lpv_type] if lpv_type else [])
    leads = event_counts(row, lead_types)
    ev = row.get("eventValue", {}) or {}
    purchases = event_counts(row, [purchase_type] if purchase_type else [])
    revenue = (ev.get(purchase_type, 0) / 100) if purchase_type else 0.0
    roas = (revenue / cost) if cost else 0.0
    return {"cost": cost, "click": click, "lpv": lpv, "leads": leads,
            "purchases": purchases, "revenue": revenue, "roas": roas}


def campaign_online_status(account: dict) -> str:
    camps = nb.get_campaigns(account["ad_account_id"], page_size=100).get("list", [])
    camp = next((c for c in camps if c["id"] == account["campaign_id"]), None)
    return camp.get("status", "UNKNOWN") if camp else "UNKNOWN"


def set_campaign_status(account: dict, status: str, execute: bool) -> None:
    if execute:
        nb.update_campaign_status(account["campaign_id"], status)


def evaluate_layer_1(today: dict) -> Optional[str]:
    min_spend = ROAS_THROTTLE_MIN_SPEND_USD
    if today["purchases"] == 0:
        min_spend *= ROAS_THROTTLE_ZERO_PURCHASE_MULTIPLIER
    if today["cost"] >= min_spend and today["roas"] < ROAS_THROTTLE_FLOOR:
        note = " (0 purchases -- doubled bar)" if today["purchases"] == 0 else ""
        return (f"ROAS-vs-spend throttle: ${today['cost']:.2f} spent today (bar ${min_spend:.2f}"
                f"{note}), ROAS {today['roas']:.2f} < floor {ROAS_THROTTLE_FLOOR}")
    return None


def evaluate_layer_2(today: dict, baseline: dict) -> Optional[str]:
    if today["click"] < LPV_CRASH_MIN_CLICKS:
        return None
    baseline_rate = max(
        (baseline["lpv"] / baseline["click"]) if baseline["click"] else 0.0,
        MIN_LPV_BASELINE_RATIO,
    )
    today_rate = today["lpv"] / today["click"]
    if today_rate < baseline_rate * LPV_CRASH_RATIO:
        return (f"Click->LPV crash: {today['click']} clicks, {today['lpv']} LPVs "
                f"today ({today_rate:.0%}) vs trailing baseline {baseline_rate:.0%}")
    return None


def evaluate_layer_3(account: dict, today: dict, baseline: dict) -> Optional[str]:
    if not account.get("lead_event_types"):
        return None
    if today["lpv"] < LEAD_CRASH_MIN_LPV:
        return None
    baseline_rate = max(
        (baseline["leads"] / baseline["lpv"]) if baseline["lpv"] else 0.0,
        MIN_LEAD_BASELINE_RATIO,
    )
    today_rate = today["leads"] / today["lpv"]
    if today_rate < baseline_rate * LEAD_CRASH_RATIO:
        return (f"LPV->Lead crash: {today['lpv']} LPVs, {today['leads']} leads "
                f"today ({today_rate:.0%}) vs trailing baseline {baseline_rate:.0%}")
    return None


def evaluate_layer_4(account: dict, today: dict, baseline: dict) -> Optional[str]:
    if not account.get("lead_event_types"):
        return None
    if today["leads"] < PURCHASE_CRASH_MIN_LEADS:
        return None
    baseline_rate = (baseline["purchases"] / baseline["leads"]) if baseline["leads"] else 0.0
    today_rate = today["purchases"] / today["leads"]
    if today_rate < baseline_rate * PURCHASE_CRASH_RATIO:
        return (f"Lead->Purchase crash: {today['leads']} leads, {today['purchases']} "
                f"purchases today ({today_rate:.0%}) vs trailing baseline {baseline_rate:.0%}")
    return None


LAYER_EVALUATORS = {
    "roas_spend_throttle": lambda account, today, baseline: evaluate_layer_1(today),
    "lpv_rate_crash": lambda account, today, baseline: evaluate_layer_2(today, baseline),
    "lead_rate_crash": lambda account, today, baseline: evaluate_layer_3(account, today, baseline),
    "purchase_rate_crash": lambda account, today, baseline: evaluate_layer_4(account, today, baseline),
}


def process_account(account_key: str, execute: bool, verbose_log: bool) -> None:
    account = config.get_account(account_key)
    if "campaign_id" not in account:
        print(f"{account_key}: no campaign_id configured, skipping.")
        return

    state = load_state(account_key)
    incidents = state.setdefault("incidents", {})
    today_str = utc_today_str()

    today = fetch_window(account, "TODAY")
    baseline = fetch_window(account, "LAST_7_DAYS")
    current_status = campaign_online_status(account)

    print(f"\n{'=' * 70}\n{account_key} (campaign {account['campaign_id']})\n{'=' * 70}")
    print(f"  Today: spend=${today['cost']:.2f} clicks={today['click']} lpv={today['lpv']} "
          f"leads={today['leads']} purchases={today['purchases']} ROAS={today['roas']:.2f}")
    print(f"  Campaign status: {current_status}")

    # Reset any incident whose date isn't today (unconditional midnight backstop)
    for tag, incident in list(incidents.items()):
        if incident.get("date") != today_str:
            print(f"  Midnight backstop: resuming {tag} from {incident.get('date')}")
            set_campaign_status(account, "ON", execute)
            log_line(account_key, f"RESUMED (midnight backstop) -- was paused by {tag} "
                                   f"on {incident.get('date')}", not execute)
            del incidents[tag]

    any_tripped = False
    for tag, evaluator in LAYER_EVALUATORS.items():
        reason = evaluator(account, today, baseline)
        incident = incidents.get(tag)

        if reason:
            any_tripped = True
            if incident is None:
                print(f"  TRIP [{tag}]: {reason}")
                set_campaign_status(account, "OFF", execute)
                incidents[tag] = {"date": today_str, "reason": reason, "retests": 0}
                log_line(account_key, f"PAUSED [{tag}] -- {reason}", not execute)
            elif incident["retests"] < MAX_REAL_RETESTS_PER_DAY:
                incident["retests"] += 1
                print(f"  STILL TRIPPED [{tag}] (retest {incident['retests']}/"
                      f"{MAX_REAL_RETESTS_PER_DAY}): {reason}")
                if verbose_log:
                    log_line(account_key, f"still paused [{tag}], retest "
                                           f"{incident['retests']}: {reason}", not execute)
            else:
                if verbose_log:
                    log_line(account_key, f"still paused [{tag}], retests exhausted "
                                          f"for today -- waiting for midnight backstop", not execute)
                print(f"  Retests exhausted for [{tag}], waiting for midnight backstop")
        elif incident is not None:
            print(f"  RECOVERED [{tag}]: no longer tripping")
            set_campaign_status(account, "ON", execute)
            log_line(account_key, f"RESUMED [{tag}] -- recovered same-day", not execute)
            del incidents[tag]
        elif verbose_log:
            print(f"  healthy [{tag}]")
            log_line(account_key, f"healthy [{tag}]", not execute)

    if not any_tripped and not incidents:
        print("  All layers healthy.")

    if execute:
        save_state(account_key, state)
    else:
        print("  [DRY RUN] state file not written.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="Named account from accounts.json (e.g. RF), "
                                        "or 'all' for every account with a campaign_id.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verbose-log", action="store_true")
    args = parser.parse_args()

    if args.account.lower() == "all":
        accounts = config.load_accounts()
        keys = [k for k, v in accounts.items() if "campaign_id" in v]
    else:
        keys = [args.account]

    print(f"Mode: {'EXECUTE (real writes)' if args.execute else 'DRY RUN (no changes will be made)'}")
    for key in keys:
        process_account(key, args.execute, args.verbose_log)


if __name__ == "__main__":
    main()
