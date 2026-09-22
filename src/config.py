"""Loads accounts.json -- short account keys (RF, HVAC, Nutra, ...) mapped
to NewsBreak org/ad-account/campaign/ad-set IDs, so scripts can take
--account RF instead of raw numeric IDs. Same role as meta-ads-automation's
own config.py, adapted to NewsBreak's flatter (no page_ids/source_accounts)
structure.

HVAC and Nutra currently only have ad_account_id set -- neither has an
established campaign/ad-set to launch into yet (HVAC has no campaigns at
all; Nutra's two campaigns don't follow RF's naming/structure), so
campaign_id/adset_id are left unset for those rather than guessed. Add
them once a real launch target exists for those accounts.
"""
import json
import subprocess
from pathlib import Path
from typing import Optional

ACCOUNTS_PATH = Path(__file__).resolve().parent.parent / "accounts.json"
PROJECT_ROOT = ACCOUNTS_PATH.parent


def load_accounts() -> dict:
    if not ACCOUNTS_PATH.is_file():
        return {}
    return json.loads(ACCOUNTS_PATH.read_text())


def get_account(key: str) -> dict:
    accounts = load_accounts()
    if key not in accounts:
        raise SystemExit(
            f"Unknown account '{key}' -- not found in {ACCOUNTS_PATH}. "
            f"Known accounts: {', '.join(accounts) or '(none)'}"
        )
    return accounts[key]


def set_field_if_unset(key: str, field: str, value: str) -> bool:
    """Writes accounts[key][field] = value only if not already set (never
    clobbers a real, already-configured value). Returns whether it wrote."""
    accounts = load_accounts()
    if accounts.get(key, {}).get(field):
        return False
    accounts.setdefault(key, {})[field] = value
    ACCOUNTS_PATH.write_text(json.dumps(accounts, indent=2) + "\n")
    return True


def get_adset_ids(key: str) -> list[str]:
    """Every ad set ever created for this account, in creation order -- the
    first is the account's original ad set, any later ones are overflow ad
    sets auto-created once an earlier one neared NewsBreak's 50-ads-per-ad-set
    cap (added 2026-09-03, see launch_ads.py's create_overflow_adset())."""
    return get_account(key).get("adset_ids", [])


def current_adset_id(key: str) -> str | None:
    """The ad set currently being filled for this account -- the last one
    in adset_ids (most recently created), or None if it has no ad set yet."""
    ids = get_adset_ids(key)
    return ids[-1] if ids else None


def append_adset_id(key: str, new_id: str) -> None:
    """Records a newly-created (overflow) ad set for this account."""
    accounts = load_accounts()
    accounts.setdefault(key, {}).setdefault("adset_ids", []).append(new_id)
    ACCOUNTS_PATH.write_text(json.dumps(accounts, indent=2) + "\n")


def git_commits_behind() -> Optional[int]:
    """Best-effort git fetch + how many commits HEAD is behind its remote
    tracking branch. Returns None on any failure (no network, no git repo,
    no upstream configured, anything) -- never raises. Ported directly from
    meta-ads-automation's own config.py (same signature, same convention:
    used by newsbreak-session-check's script to build a structured
    pass/fail report)."""
    try:
        subprocess.run(
            ["git", "fetch", "-q"],
            cwd=PROJECT_ROOT, timeout=15, capture_output=True, check=True,
        )
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}"],
            cwd=PROJECT_ROOT, timeout=10, capture_output=True, text=True, check=True,
        )
        return int(result.stdout.strip())
    except Exception:
        return None
