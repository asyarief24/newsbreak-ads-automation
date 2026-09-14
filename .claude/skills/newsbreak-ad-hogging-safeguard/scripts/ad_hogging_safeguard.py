#!/usr/bin/env python
"""Runs on a schedule (see .github/workflows/ad-hogging-safeguard.yml) across
a named account's two ad sets, pausing any single ad that's taking more than
its fair share of that ad set's daily budget while performing badly today --
so other ads sharing the same budget get a fair chance. Ported from
meta-ads-automation's meta-ad-hogging-safeguard (same same-day throttle +
lifetime-backstop design, same tag/resume state machinery) -- adapted for a
materially different scale, NOT a blind copy of its dollar thresholds.

Unlike Meta's Campaign A (one shared pool ad set), each account here has TWO
ad sets, each with its OWN daily budget -- so "fair share" is computed
per-ad-set (budget / active ad count in that ad set), and every check below
runs independently for each of the account's two ad sets.

--- Real scale, pulled live 2026-09-14 (fair share = ad-set daily budget /
    active ad count in that ad set) ---

Account    | ad set budget/day | active ads | fair share/ad/day
RF         | $40 / $40         | 52 / 58    | $0.77 / $0.69
HVAC       | $25 / $25         | 52 / 41    | $0.48 / $0.61
Bathroom   | $25 / $25         | 42 / 27    | $0.60 / $0.93
Siding     | $25 / $25         | 44 / 29    | $0.57 / $0.86
Flooring   | $20 / $20         | 43 / 25    | $0.47 / $0.80

This is roughly 50-100x smaller than Meta's Campaign A fair share ($40-50/ad/
day there), so this script's thresholds are new numbers derived from the
table above, not meta-ad-hogging-safeguard's $50/$100/$200/$100 figures.
Account-wide daily leads are only single digits and purchases are 0-4 (see
newsbreak-campaign-safeguard's own docstring for the full account table) --
split across 25-58 ads per ad set, meaning most INDIVIDUAL ads will show 0
leads/purchases on any given day. The same-day per-ad throttle will
therefore mostly be evidence-starved day to day; the lifetime backstop
(accumulated over many days) is expected to be this script's primary real
enforcement mechanism at this scale, exactly as it ended up being the
dominant path in meta-ad-hogging-safeguard too, just more so here.

Same-day throttle, evaluated per ad within its own ad set:

  PAUSE this ad if ALL of:
    - ad is currently ON
    - today's spend > AD_HOG_FAIR_SHARE_MULTIPLIER (5x) * this ad set's own
      fair share, AND >= AD_HOG_MIN_SPEND_FLOOR_USD ($3 -- a floor so a
      single extra click on a $0.50 fair-share ad set can't trip this on
      pure noise)
    - underperforming, judged on whichever signal the ad actually has (best
      evidence first, same escalation meta-ad-hogging-safeguard settled on
      2026-08-23):
        - >= AD_MIN_PURCHASES_FOR_ROAS (2) purchases today -> ROAS < 1.0
        - else >= AD_MIN_LEADS_FOR_CPL (2) leads today, AND the ad set's
          own pool has >= POOL_MIN_LEADS_FOR_CPL_BENCHMARK (5) leads today
          -> this ad's CPL > 1.5x the pool's own CPL today
        - else 0 leads today -> that IS the verdict
        - else (1 lead, no benchmark) -> no action, not enough signal
    - hasn't already used its MAX_TOGGLES_PER_DAY (2) pause/resume round
      trips today

Resume: passive, re-evaluated every run -- ROAS/CPL recovers, or an
unconditional forced resume at 00:00 UTC (no per-account timezone lookup
exists in this project yet, unlike Meta's; see newsbreak-campaign-safeguard
for the same simplification).

Lifetime backstop (separate, permanent kill, tracked in state["lifetime_
kills"]): any ad currently ON tripping any of:
  1. no-lead: lifetime spend > $10, zero leads ever (skipped for an account
     with no lead_event_types configured, e.g. RF)
  2. dud: lifetime spend > $15, zero purchases ever
  3. bad-ROAS: lifetime spend > $25, lifetime ROAS < 0.5

"Lifetime" here means a 30-day trailing window (NewsBreak's report API caps
useful lookback at LAST_30_DAYS) -- these accounts are only ~2 weeks old as
of this writing, so 30 days is effectively their whole history so far; this
will need to become a genuine rolling window once accounts are older.

Same-day resume-for-late-attribution: re-runs the three lifetime conditions
every pass for anything in lifetime_kills that shows spend > 0 today,
reactivating if none still trip (a late postback landing after the kill).

No notification of any kind, fully autonomous. Logs only to
logs/ad-safeguards/<UTC date>.md, only when something actually happens.
Dry-run by default; --execute makes real writes.

These are a FIRST PASS calibrated from real 2026-09-14 data, not burned in
over weeks -- revisit as more data comes in, same caveat as
newsbreak-campaign-safeguard.
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

STATE_DIR = PROJECT_ROOT / "ad_safeguard_state"
LOG_DIR = PROJECT_ROOT / "logs" / "ad-safeguards"

AD_HOG_FAIR_SHARE_MULTIPLIER = 5.0
AD_HOG_MIN_SPEND_FLOOR_USD = 3.0
AD_ROAS_THRESHOLD = 1.0
AD_MIN_PURCHASES_FOR_ROAS = 2
AD_MIN_LEADS_FOR_CPL = 2
POOL_MIN_LEADS_FOR_CPL_BENCHMARK = 5
CPL_RATIO_THRESHOLD = 1.5
MAX_TOGGLES_PER_DAY = 2

LIFETIME_NO_LEAD_SPEND_THRESHOLD_USD = 10.0
LIFETIME_DUD_SPEND_THRESHOLD_USD = 15.0
LIFETIME_BAD_ROAS_SPEND_THRESHOLD_USD = 25.0
LIFETIME_BAD_ROAS_THRESHOLD = 0.5
LIFETIME_WINDOW_DATE_RANGE = "LAST_30_DAYS"


def utc_today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_state(account_key: str) -> dict:
    path = STATE_DIR / f"{account_key}.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {"incidents": {}, "lifetime_kills": {}}


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


def fetch_per_ad_metrics(account: dict, adset_id: str, date_range: str) -> dict[str, dict]:
    """{ad_id: {cost, click, leads, purchases, revenue, roas}} for one ad set."""
    lead_types = account.get("lead_event_types", [])
    purchase_type = account.get("purchase_event_type")
    event_metrics = []
    for et in filter(None, [purchase_type, *lead_types]):
        event_metrics.append({"eventType": et, "metrics": ["COUNT", "VALUE"]})
    data = nb.get_report(
        account["ad_account_id"], dimensions=["AD"], metrics=["COST", "CLICK"],
        date_range=date_range, event_metrics=event_metrics or None,
        filter_type="AD_SET", filter_ids=[adset_id],
    )
    out = {}
    for row in data.get("rows", []):
        cost = row.get("cost", 0) / 100
        leads = event_counts(row, lead_types)
        purchases = event_counts(row, [purchase_type] if purchase_type else [])
        ev = row.get("eventValue", {}) or {}
        revenue = (ev.get(purchase_type, 0) / 100) if purchase_type else 0.0
        out[row["adId"]] = {
            "cost": cost, "click": row.get("click", 0), "leads": leads,
            "purchases": purchases, "revenue": revenue,
            "roas": (revenue / cost) if cost else 0.0,
        }
    return out


def fair_share_for_adset(account: dict, adset_id: str) -> tuple[float, int]:
    adsets = nb.get_ad_sets(account["ad_account_id"], campaign_ids=[account["campaign_id"]],
                             page_size=100).get("list", [])
    adset = next((a for a in adsets if a["id"] == adset_id), None)
    budget = (adset.get("budget", 0) / 100) if adset else 0.0
    ads = nb.get_ads(account["ad_account_id"], ad_set_ids=[adset_id], page_size=500).get("list", [])
    active_count = max(1, sum(1 for a in ads if a.get("onlineStatus") == "ACTIVE"))
    return budget / active_count, active_count


def verdict_for_ad(account: dict, ad_metrics: dict, pool_cpl: Optional[float]) -> Optional[str]:
    purchases = ad_metrics["purchases"]
    leads = ad_metrics["leads"]
    if purchases >= AD_MIN_PURCHASES_FOR_ROAS:
        if ad_metrics["roas"] < AD_ROAS_THRESHOLD:
            return f"ROAS {ad_metrics['roas']:.2f} < {AD_ROAS_THRESHOLD} on {purchases} purchases"
        return None
    if leads >= AD_MIN_LEADS_FOR_CPL and pool_cpl:
        cpl = ad_metrics["cost"] / leads
        if cpl > pool_cpl * CPL_RATIO_THRESHOLD:
            return f"CPL ${cpl:.2f} > {CPL_RATIO_THRESHOLD}x pool CPL ${pool_cpl:.2f}"
        return None
    if leads == 0:
        return "0 leads today"
    return None  # 1 lead, no benchmark -- not enough signal


def process_adset(account_key: str, account: dict, adset_id: str, state: dict,
                   execute: bool, verbose_log: bool) -> None:
    incidents = state.setdefault("incidents", {})
    lifetime_kills = state.setdefault("lifetime_kills", {})
    today_str = utc_today_str()

    fair_share, active_count = fair_share_for_adset(account, adset_id)
    hog_bar = max(fair_share * AD_HOG_FAIR_SHARE_MULTIPLIER, AD_HOG_MIN_SPEND_FLOOR_USD)
    today_metrics = fetch_per_ad_metrics(account, adset_id, "TODAY")
    lifetime_metrics = fetch_per_ad_metrics(account, adset_id, LIFETIME_WINDOW_DATE_RANGE)
    ads = nb.get_ads(account["ad_account_id"], ad_set_ids=[adset_id], page_size=500).get("list", [])
    ad_status = {a["id"]: a.get("status", "OFF") for a in ads}

    pool_leads_today = sum(m["leads"] for m in today_metrics.values())
    pool_cost_today = sum(m["cost"] for m in today_metrics.values())
    pool_cpl = (pool_cost_today / pool_leads_today) if pool_leads_today >= POOL_MIN_LEADS_FOR_CPL_BENCHMARK else None

    print(f"\n  Ad set {adset_id}: fair_share=${fair_share:.2f}/day, hog_bar=${hog_bar:.2f}, "
          f"{active_count} active ads, pool_leads_today={pool_leads_today}, "
          f"pool_cpl={'$%.2f' % pool_cpl if pool_cpl else 'n/a'}")

    # --- Midnight backstop for same-day incidents ---
    for ad_id, incident in list(incidents.items()):
        if incident.get("adset_id") != adset_id:
            continue
        if incident.get("date") != today_str:
            print(f"    Midnight backstop: resuming ad {ad_id}")
            if execute:
                nb.update_ad_status(ad_id, "ON")
            log_line(account_key, f"RESUMED ad {ad_id} (midnight backstop, was paused "
                                   f"{incident.get('date')})", not execute)
            del incidents[ad_id]

    # --- Same-day resume-for-late-attribution on lifetime kills ---
    for ad_id, kill in list(lifetime_kills.items()):
        if kill.get("adset_id") != adset_id:
            continue
        today_m = today_metrics.get(ad_id)
        if not today_m or today_m["cost"] <= 0 or ad_status.get(ad_id) != "OFF":
            continue
        life_m = lifetime_metrics.get(ad_id, {"cost": 0, "leads": 0, "purchases": 0, "roas": 0})
        still_trips = (
            (account.get("lead_event_types") and life_m["cost"] > LIFETIME_NO_LEAD_SPEND_THRESHOLD_USD and life_m["leads"] == 0)
            or (life_m["cost"] > LIFETIME_DUD_SPEND_THRESHOLD_USD and life_m["purchases"] == 0)
            or (life_m["cost"] > LIFETIME_BAD_ROAS_SPEND_THRESHOLD_USD and life_m["roas"] < LIFETIME_BAD_ROAS_THRESHOLD)
        )
        if not still_trips:
            print(f"    Late-attribution resume: ad {ad_id} no longer trips lifetime backstop")
            if execute:
                nb.update_ad_status(ad_id, "ON")
            log_line(account_key, f"RESUMED ad {ad_id} -- lifetime kill undone "
                                   f"(late-attributing conversion)", not execute)
            del lifetime_kills[ad_id]

    # --- Same-day fair-share throttle ---
    for ad_id, m in today_metrics.items():
        if ad_status.get(ad_id) != "ON":
            continue
        if m["cost"] <= hog_bar:
            continue
        reason = verdict_for_ad(account, m, pool_cpl)
        if not reason:
            continue
        incident = incidents.get(ad_id)
        full_reason = f"${m['cost']:.2f} spent (bar ${hog_bar:.2f}), {reason}"
        if incident is None:
            print(f"    TRIP ad {ad_id}: {full_reason}")
            if execute:
                nb.update_ad_status(ad_id, "OFF")
            incidents[ad_id] = {"adset_id": adset_id, "date": today_str, "toggles": 0}
            log_line(account_key, f"PAUSED ad {ad_id} -- {full_reason}", not execute)
        elif incident["toggles"] < MAX_TOGGLES_PER_DAY:
            print(f"    still hogging, ad {ad_id}: {full_reason}")
        elif verbose_log:
            log_line(account_key, f"ad {ad_id} still hogging, toggles exhausted: "
                                   f"{full_reason}", not execute)

    # --- Passive resume for same-day incidents whose ad recovered ---
    for ad_id, incident in list(incidents.items()):
        if incident.get("adset_id") != adset_id or incident.get("date") != today_str:
            continue
        m = today_metrics.get(ad_id)
        if not m:
            continue
        reason = verdict_for_ad(account, m, pool_cpl)
        if not reason and m["cost"] > 0:
            print(f"    RECOVERED ad {ad_id}")
            incident["toggles"] += 1
            if incident["toggles"] <= MAX_TOGGLES_PER_DAY:
                if execute:
                    nb.update_ad_status(ad_id, "ON")
                log_line(account_key, f"RESUMED ad {ad_id} -- recovered same-day", not execute)
                del incidents[ad_id]

    # --- Lifetime backstop (permanent kill) ---
    for ad_id, m in lifetime_metrics.items():
        if ad_status.get(ad_id) != "ON" or ad_id in lifetime_kills:
            continue
        reason = None
        if account.get("lead_event_types") and m["cost"] > LIFETIME_NO_LEAD_SPEND_THRESHOLD_USD and m["leads"] == 0:
            reason = f"no-lead: ${m['cost']:.2f} lifetime, 0 leads"
        elif m["cost"] > LIFETIME_DUD_SPEND_THRESHOLD_USD and m["purchases"] == 0:
            reason = f"dud: ${m['cost']:.2f} lifetime, 0 purchases"
        elif m["cost"] > LIFETIME_BAD_ROAS_SPEND_THRESHOLD_USD and m["roas"] < LIFETIME_BAD_ROAS_THRESHOLD:
            reason = f"bad-ROAS: ${m['cost']:.2f} lifetime, ROAS {m['roas']:.2f}"
        if reason:
            print(f"    LIFETIME KILL ad {ad_id}: {reason}")
            if execute:
                nb.update_ad_status(ad_id, "OFF")
            lifetime_kills[ad_id] = {"adset_id": adset_id, "date": today_str, "reason": reason}
            log_line(account_key, f"KILLED ad {ad_id} (lifetime backstop) -- {reason}", not execute)


def process_account(account_key: str, execute: bool, verbose_log: bool) -> None:
    account = config.get_account(account_key)
    adset_ids = account.get("adset_ids", [])
    if not adset_ids:
        print(f"{account_key}: no adset_ids configured, skipping.")
        return

    state = load_state(account_key)
    print(f"\n{'=' * 70}\n{account_key}\n{'=' * 70}")
    for adset_id in adset_ids[:2]:  # this account's original two ad sets, per accounts.json
        process_adset(account_key, account, adset_id, state, execute, verbose_log)

    if execute:
        save_state(account_key, state)
    else:
        print("\n  [DRY RUN] state file not written.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="Named account from accounts.json (e.g. RF), "
                                        "or 'all' for every account with adset_ids.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verbose-log", action="store_true")
    args = parser.parse_args()

    if args.account.lower() == "all":
        accounts = config.load_accounts()
        keys = [k for k, v in accounts.items() if v.get("adset_ids")]
    else:
        keys = [args.account]

    print(f"Mode: {'EXECUTE (real writes)' if args.execute else 'DRY RUN (no changes will be made)'}")
    for key in keys:
        process_account(key, args.execute, args.verbose_log)


if __name__ == "__main__":
    main()
