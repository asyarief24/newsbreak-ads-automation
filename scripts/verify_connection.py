"""Simplest possible real API call: confirms NEWSBREAK_ACCESS_TOKEN works
by fetching the organizations you admin, then the ad accounts under them.

Usage:
    python scripts/verify_connection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from newsbreak_api import get_admin_orgs, get_ad_accounts  # noqa: E402


def main() -> None:
    orgs = get_admin_orgs()
    if not orgs:
        print("Connected, but this token's user has no ORG_ADMIN organizations.")
        return

    print(f"Found {len(orgs)} organization(s):")
    for org in orgs:
        print(f"  - {org['name']} (id={org['id']})")

    org_ids = [org["id"] for org in orgs]
    groups = get_ad_accounts(org_ids)
    for group in groups:
        print(f"\nOrg '{group['name']}' (id={group['id']}) ad accounts:")
        for acct in group.get("adAccounts", []):
            print(f"  - {acct['name']} (id={acct['id']})")


if __name__ == "__main__":
    main()
