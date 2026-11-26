"""Leaf script for the cross-platform-overview skill -- NewsBreak side.

Run FROM this project's root (needs `from src import ...` to resolve):
    python .claude/skills/cross-platform-overview/scripts/newsbreak_metrics.py --since 2026-09-10 --until 2026-09-16

Prints one JSON object to stdout: {account_key: {account_name, spend, clicks, sold_leads, revenue}}.

"Sold leads" / revenue = each account's own `purchase_event_type`
(accounts.json), the real payment/revenue event -- NOT `lead_event_types`,
which is an earlier-funnel form-fill/registration signal, same
distinction Meta's "Sold Lead" pixel vs. plain lead pixel makes.

`Nutra` is deliberately excluded -- confirmed 2026-09-17 the user wants it
out of this report (it also has no purchase_event_type configured at all,
being a different vertical from the 5 home-services accounts this report
is really about).

Both `get_report` calls pass `timezone="America/New_York"` explicitly --
confirmed live 2026-11-26 that NewsBreak's API otherwise defaults to UTC,
while Meta's own Insights calls (meta_metrics.py, the sibling leaf script)
use each ad account's own configured reporting timezone, confirmed
America/New_York for every account here. Without this override, a single-
day report's "day" boundary could differ by up to 4-5 hours between the
two platforms' numbers for what's supposed to be the same calendar date.
Google Ads needs no equivalent fix -- `segments.date` is tied to each
customer account's own configured timezone (not a query param), and every
account's own timezone is already confirmed America/New_York.
"""
import argparse
import json

from src import config, newsbreak_api as api

EXCLUDED_ACCOUNTS = {"Nutra"}


def resolve_account_names(accounts: dict) -> dict:
    """ad_account_id -> real ad account name, e.g. 'AG | HVAC'. Needed
    because a zero-spend account has no report row to read a name off of."""
    org_ids = list({a["org_id"] for a in accounts.values() if "org_id" in a})
    if not org_ids:
        return {}
    groups = api.get_ad_accounts(org_ids)
    names = {}
    for group in groups:
        for acc in group.get("adAccounts", []):
            names[str(acc["id"])] = acc["name"]
    return names


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", required=True, help="YYYY-MM-DD, inclusive")
    args = p.parse_args()

    out = {}
    accounts = config.load_accounts()
    account_names = resolve_account_names(accounts)
    for name, account in accounts.items():
        if name in EXCLUDED_ACCOUNTS:
            continue
        try:
            ad_account_id = account["ad_account_id"]
            purchase_type = account.get("purchase_event_type")
            account_name = account_names.get(ad_account_id, name)

            if not purchase_type:
                report = api.get_report(
                    ad_account_id, dimensions=["AD_ACCOUNT"], metrics=["COST", "CLICK"],
                    date_range="FIXED", start_date=args.since, end_date=args.until,
                    timezone="America/New_York",
                )
                rows = report.get("rows", [])
                spend = sum(float(r.get("costDecimal", r.get("cost", 0)) or 0) for r in rows) / 100
                clicks = sum(int(r.get("click", 0) or 0) for r in rows)
                out[name] = {"account_name": account_name, "spend": round(spend, 2),
                              "clicks": clicks, "sold_leads": None, "revenue": None,
                              "note": "no purchase_event_type configured"}
                continue

            report = api.get_report(
                ad_account_id, dimensions=["AD_ACCOUNT"], metrics=["COST", "CLICK"],
                date_range="FIXED", start_date=args.since, end_date=args.until,
                event_metrics=[{"eventType": purchase_type, "metrics": ["COUNT", "VALUE"]}],
                timezone="America/New_York",
            )
            rows = report.get("rows", [])
            spend = 0.0
            clicks = 0
            sold_leads = 0
            revenue = 0.0
            for row in rows:
                spend += float(row.get("costDecimal", row.get("cost", 0)) or 0) / 100
                clicks += int(row.get("click", 0) or 0)
                ec = row.get("eventCount", {}) or {}
                ev = row.get("eventValue", {}) or {}
                sold_leads += int(ec.get(purchase_type, 0) or 0)
                revenue += float(ev.get(purchase_type, 0) or 0) / 100
            out[name] = {
                "account_name": account_name,
                "spend": round(spend, 2),
                "clicks": clicks,
                "sold_leads": sold_leads,
                "revenue": round(revenue, 2),
            }
        except Exception as e:
            out[name] = {"account_name": name, "error": str(e)}

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
