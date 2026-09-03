# Setup — getting real API access

Much simpler than Google Ads or Meta — a single access token, no OAuth.

## 1. Create an ad account

In NewsBreak Ad Manager, make sure you have an ad account created.

## 2. Generate an API access token

In Ad Manager: **Resources → API Access Tokens → Generate Token**.

## 3. Save it

```bash
cp .env.example .env
```

Paste the token into `.env` as `NEWSBREAK_ACCESS_TOKEN`. `.env` is
gitignored — never commit it.

## 4. Verify

```bash
pip install -r requirements.txt
python scripts/verify_connection.py
```

This calls `/org/admin-orgs` then `/ad-account/getGroupsByOrgIds` and
prints every organization and ad account the token can see — confirms
the token works before we build anything real on top of it.

## Reference

- **Base URL:** `https://business.newsbreak.com/business-api/v1`
- **Auth:** `Access-Token` header on every request, no OAuth/refresh flow
- **Structure:** Organization → Ad Account → Campaign → Ad Set → Ad
  (budgets in cents) — same shape as Meta's, unlike Google Ads
- **Objectives:** `WEB_CONVERSION`, `APP_CONVERSION`, `REACH`,
  `WEB_TRAFFIC`, `APP_TRAFFIC`
- **Conversion tracking:** Events (`PIXEL` or `POSTBACK` type), created
  per ad account, referenced by ID in an ad set — analogous to Meta's
  pixel/CAPI custom conversions
- **Reporting:** a synchronous report endpoint plus async custom-report
  create/fetch-by-id endpoints
- **Rate limits:** tiered by app (Basic/Advanced/Premium), each with a
  QPS/QPM/QPD cap; a `code: 4034` response means throttled — `src/newsbreak_api.py`
  already retries with backoff on that code for individual calls, but a
  QPD-level throttle needs to wait until 00:00 UTC, which isn't handled
  automatically
- **Full API reference:** https://advertising-api.newsbreak.com/hc/en-us
  (Campaign, Ad Set, Ad, Targeting, Report, Audience, Account Billing,
  Event Management sections — `src/newsbreak_api.py` only implements the
  handful of endpoints verified so far; add more following the same
  `_get`/`_post` pattern as the actual build needs them)
