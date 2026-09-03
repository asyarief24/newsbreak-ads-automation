"""Thin wrapper around the NewsBreak Advertising API
(https://advertising-api.newsbreak.com/hc/en-us).

Auth is a single Access-Token header (no OAuth) -- generated in Ad
Manager under Resources -> API Access Tokens. Every endpoint returns
{"code": int, "errMsg": str, "data": ...}; code 0 is success, code 4034
means rate-limited (see docs/rate-limits.md).

Only the endpoints actually verified against the docs are implemented
below -- add more following the same `_get`/`_post` pattern as needed
rather than guessing a shape ahead of time.
"""
import os
import time

import requests
from dotenv import load_dotenv

BASE_URL = "https://business.newsbreak.com/business-api/v1"
RATE_LIMIT_CODE = 4034
# NewsBreak's documented rate-limit code is 4034, but confirmed live,
# 2026-09-03: sustained heavy write traffic (multiple accounts' large
# batches over several hours) made /ad/create fail with a completely
# different, undocumented shape instead -- {"code": -1, "errMsg":
# "Invalid operation. Please reach out to your Account Manager..."} --
# 100% of a 66-video batch failed with this exact message (even with a
# 3-attempt/5s retry already in place at the call site), while isolated
# single manual create_ad calls immediately before and after each kept
# succeeding. That pattern (every call in a tight back-to-back loop
# fails, every naturally-spaced-out standalone call succeeds) points at
# an undocumented write-endpoint throttle surfaced via a generic message
# rather than the documented code -- not a real, permanent validation
# error. Retried the same as RATE_LIMIT_CODE, keyed on message text since
# there's no dedicated code for it; deliberately narrow (exact phrase
# match) so a genuinely different -1 error (e.g. "illegal event
# tracking", a real permanent validation failure seen earlier the same
# day) is never retried into wasted attempts.
RATE_LIMIT_MESSAGE_SUBSTRING = "Invalid operation"
RETRY_BACKOFF_SECONDS = [5, 15, 30, 60]  # longer than the old 2**attempt (1,2,4,8s) --
                                          # confirmed live that 5s between attempts
                                          # wasn't enough to clear this throttle.


class NewsbreakAPIError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"NewsBreak API error {code}: {message}")


def _headers() -> dict:
    load_dotenv()
    token = os.environ.get("NEWSBREAK_ACCESS_TOKEN")
    if not token:
        raise SystemExit("NEWSBREAK_ACCESS_TOKEN not set in .env")
    return {"Content-Type": "application/json", "Access-Token": token}


def _should_retry(body: dict) -> bool:
    if body.get("code") == RATE_LIMIT_CODE:
        return True
    return body.get("code") == -1 and RATE_LIMIT_MESSAGE_SUBSTRING in body.get("errMsg", "")


def _handle_response(resp: requests.Response) -> dict:
    """Checks the JSON body's own {code, errMsg} first -- even on a 4xx/5xx
    HTTP status, NewsBreak's real error detail lives there, not in the
    generic HTTP reason phrase. Only falls back to raise_for_status() (a
    less informative error) when the body isn't JSON at all.
    """
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    if body.get("code") not in (None, 0):
        raise NewsbreakAPIError(body.get("code"), body.get("errMsg", "unknown error"))
    resp.raise_for_status()
    return body.get("data", {})


def _get(path: str, params: dict | None = None, max_retries: int = 5) -> dict:
    for attempt in range(max_retries):
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=30)
        body = resp.json()
        if _should_retry(body) and attempt < max_retries - 1:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue
        return _handle_response(resp)
    return _handle_response(resp)


def _post(path: str, json_body: dict, max_retries: int = 5) -> dict:
    for attempt in range(max_retries):
        resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), json=json_body, timeout=30)
        body = resp.json()
        if _should_retry(body) and attempt < max_retries - 1:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue
        return _handle_response(resp)
    return _handle_response(resp)


def _put(path: str, json_body: dict, max_retries: int = 5) -> dict:
    for attempt in range(max_retries):
        resp = requests.put(f"{BASE_URL}{path}", headers=_headers(), json=json_body, timeout=30)
        body = resp.json()
        if _should_retry(body) and attempt < max_retries - 1:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue
        return _handle_response(resp)
    return _handle_response(resp)


# --- Organization ---

def get_admin_orgs() -> list[dict]:
    """Organizations where this token's user holds ORG_ADMIN. No params."""
    return _get("/org/admin-orgs").get("list", [])


# --- Ad Account ---

def get_ad_accounts(org_ids: list[str]) -> list[dict]:
    """Ad accounts grouped by organization, for the given org IDs."""
    return _get("/ad-account/getGroupsByOrgIds", params={"orgIds": org_ids}).get("list", [])


# --- Campaign ---

def create_campaign(ad_account_id: str, name: str, objective: str, status: str = "ON") -> dict:
    """objective: WEB_CONVERSION | APP_CONVERSION | REACH | WEB_TRAFFIC | APP_TRAFFIC"""
    return _post("/campaign/create", {
        "adAccountId": ad_account_id,
        "name": name,
        "objective": objective,
        "status": status,
    })


def get_campaigns(ad_account_id: str, online_status: list[str] | None = None,
                   search: str | None = None, page_no: int = 1, page_size: int = 100) -> dict:
    """onlineStatus values: WARNING | INACTIVE | ACTIVE | DELETED.
    Returns the full {rows, pageNo, pageSize, total, hasNext} dict (not
    just the rows) since pagination matters here."""
    params = {"adAccountId": ad_account_id, "pageNo": page_no, "pageSize": page_size}
    if online_status:
        params["onlineStatus"] = online_status
    if search:
        params["search"] = search
    return _get("/campaign/getList", params=params)


# --- Ad Set ---

def get_ad_sets(ad_account_id: str, campaign_ids: list[str] | None = None,
                 online_status: list[str] | None = None, search: str | None = None,
                 page_no: int = 1, page_size: int = 100) -> dict:
    """onlineStatus values: WARNING | INACTIVE | ACTIVE | DELETED | READY | COMPLETED."""
    params = {"adAccountId": ad_account_id, "pageNo": page_no, "pageSize": page_size}
    if campaign_ids:
        params["campaignIds"] = campaign_ids
    if online_status:
        params["onlineStatus"] = online_status
    if search:
        params["search"] = search
    return _get("/ad-set/getList", params=params)


def create_ad_set(campaign_id: str, name: str, budget_type: str, budget: int, start_time: int,
                   end_time: int, bid_type: str, bid_rate: int | None = None,
                   tracking_id: str | None = None, roas: float | None = None,
                   delivery_rate: str | None = None, targeting: dict | None = None,
                   platforms: list[str] | None = None, frequency_caps: list[dict] | None = None,
                   schedule: dict | None = None, status: str = "ON") -> dict:
    """budgetType: DAILY | TOTAL. bidType: CPM | CPC | DAY_ONE_MAX_CONVERSION_VALUE |
    MAX_CONVERSION | MAX_CONVERSION_VALUE | TARGET_CPA | TARGET_ROAS | DAY_ONE_TARGET_ROAS.
    bidRate required for CPM/CPC/TARGET_CPA. trackingId required for every
    bidType except CPM/CPC. roas required for TARGET_ROAS/DAY_ONE_TARGET_ROAS."""
    body = {
        "campaignId": campaign_id, "name": name, "budgetType": budget_type, "budget": budget,
        "startTime": start_time, "endTime": end_time, "bidType": bid_type, "status": status,
    }
    if bid_rate is not None:
        body["bidRate"] = bid_rate
    if tracking_id is not None:
        body["trackingId"] = tracking_id
    if roas is not None:
        body["roas"] = roas
    if delivery_rate is not None:
        body["deliveryRate"] = delivery_rate
    if targeting is not None:
        body["targeting"] = targeting
    if platforms is not None:
        body["platforms"] = platforms
    if frequency_caps is not None:
        body["frequencyCaps"] = frequency_caps
    if schedule is not None:
        body["schedule"] = schedule
    return _post("/ad-set/create", body)


# --- Ad ---

def get_ads(ad_account_id: str, campaign_ids: list[str] | None = None,
            ad_set_ids: list[str] | None = None, online_status: list[str] | None = None,
            search: str | None = None, page_no: int = 1, page_size: int = 100) -> dict:
    """onlineStatus values: WARNING | INACTIVE | ACTIVE | DELETED | PENDING | REJECTED."""
    params = {"adAccountId": ad_account_id, "pageNo": page_no, "pageSize": page_size}
    if campaign_ids:
        params["campaignIds"] = campaign_ids
    if ad_set_ids:
        params["adSetIds"] = ad_set_ids
    if online_status:
        params["onlineStatus"] = online_status
    if search:
        params["search"] = search
    return _get("/ad/getList", params=params)


# --- Ad: asset upload + create ---

def upload_asset(ad_account_id: str, file_path: str, save_to_media_library: bool = False,
                  media_name: str | None = None) -> dict:
    """Uploads one image/video/gif to NewsBreak's CDN. Returns {assetUrl, mediaId}."""
    load_dotenv()
    token = os.environ.get("NEWSBREAK_ACCESS_TOKEN")
    if not token:
        raise SystemExit("NEWSBREAK_ACCESS_TOKEN not set in .env")
    data = {"adAccountId": ad_account_id}
    if save_to_media_library:
        data["saveToMediaLibrary"] = "true"
        if media_name:
            data["mediaName"] = media_name
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/ad/uploadAssets",
            headers={"Access-Token": token},  # no Content-Type -- requests sets multipart boundary
            files={"asset": f},
            data=data,
            timeout=120,
        )
    return _handle_response(resp)


def create_ad(ad_set_id: str, name: str, creative: dict, status: str = "ON",
              click_tracking_url: list[str] | None = None) -> dict:
    """creative must match the /ad/create schema: type, headline, assetUrl,
    description, callToAction, brandName, plus optional coverUrl (video),
    clickThroughUrl, logoUrl."""
    body = {"adSetId": ad_set_id, "name": name, "creative": creative, "status": status}
    if click_tracking_url:
        body["clickTrackingUrl"] = click_tracking_url
    return _post("/ad/create", body)


def update_ad_status(ad_id: str, status: str) -> dict:
    """status: ON | OFF. Pausing (OFF), never deleting, matches this
    project's own convention on the Meta side (ads/ad sets are never
    deleted, only paused, so a human can always investigate afterward)."""
    return _put(f"/ad/updateStatus/{ad_id}", {"status": status})


# --- Event Management (conversion tracking) ---

def get_events(ad_account_id: str, os_filter: str | None = None) -> list[dict]:
    """os_filter: IOS | ANDROID | "" (web) | None (all)."""
    params = {} if os_filter is None else {"os": os_filter}
    return _get(f"/event/getList/{ad_account_id}", params=params).get("list", [])
