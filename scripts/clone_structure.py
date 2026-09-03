"""Clones a campaign + ad set + one active ad from a source account's
proven structure into a target account that has none yet -- e.g. seeding
HVAC's campaign/ad-set/ad from RF's working setup.

Only structural fields are cloned verbatim (budget, targeting, platforms,
frequency caps, creative brandName/logoUrl/callToAction, and the actual
media asset -- re-downloaded and re-uploaded, since NewsBreak's asset CDN
URLs are account-scoped and can't be referenced cross-account directly).

Ad copy (headline/description) is always regenerated fresh via Gemini for
the target account's own --vertical, never copied from the source (the
source's copy is written for its own vertical, e.g. "roofing quote" text
has no business on an HVAC ad).

Two modes for bidType/trackingId/clickThroughUrl/status, since a target
account with no conversion event of its own registered yet (see
`api.get_events()`) can't actually use the source's real bid type --
NewsBreak rejects ad-set creation ("illegal event tracking") for a
WEB_CONVERSION campaign's ad set without a valid trackingId, even under
a bid type that doesn't nominally require one (confirmed live):

  - Default (safe): clickThroughUrl/trackingId left unset, ad set uses a
    placeholder CPC bid instead of the source's real bid type, everything
    created OFF (paused). Confirmed this still fails today for a
    WEB_CONVERSION-objective campaign with zero events -- kept as the
    default for a target account that already has its own event.
  - --as-is: clones bidType/trackingId/clickThroughUrl/status verbatim
    from the source, unchanged except for the name. Explicit opt-in for
    "make it work now, I'll repoint tracking/URL to this account's own
    later" -- the cloned ad set/ad will report conversions and send
    clicks to the SOURCE account's own tracking/landing page until
    someone updates them for real.

Dry-run by default -- prints exactly what it would create. Pass --execute
to actually create the campaign/ad set/ad for real. On success, the new
campaign_id/adset_id are written into accounts.json for the target
account (only if not already set there -- see config.set_field_if_unset).

Usage:
    python scripts/clone_structure.py --from-account RF --to-account HVAC \
        --vertical HVAC --media-type image --as-is

    ... same args ... --execute
"""
import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
import custom_copy  # noqa: E402
import newsbreak_api as api  # noqa: E402

PLACEHOLDER_BID_RATE_CENTS = 100  # $1.00 CPC -- inconsequential, ad set is created OFF


def download(url: str, suffix: str) -> Path:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    out = Path(tempfile.gettempdir()) / f"clone_asset_{hashlib.sha256(url.encode()).hexdigest()[:12]}{suffix}"
    out.write_bytes(resp.content)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-account", required=True, help="Source account key in accounts.json.")
    parser.add_argument("--to-account", required=True, help="Target account key in accounts.json.")
    parser.add_argument("--vertical", required=True, help="Used in the target ad's copy-generation prompt.")
    parser.add_argument("--media-type", choices=["image", "video"], default="image")
    parser.add_argument("--as-is", action="store_true",
                         help="Clone bidType/trackingId/clickThroughUrl/status verbatim from the "
                              "source instead of the safe OFF/placeholder-CPC default.")
    parser.add_argument("--execute", action="store_true", help="Actually create the campaign/ad set/ad.")
    args = parser.parse_args()

    src = config.get_account(args.from_account)
    dst = config.get_account(args.to_account)
    src_adset_id = config.current_adset_id(args.from_account)
    if not src.get("campaign_id") or not src_adset_id:
        raise SystemExit(f"'{args.from_account}' has no campaign_id/adset_ids set in accounts.json.")

    print(f"Cloning {args.from_account} -> {args.to_account}  |  Mode: {'EXECUTE' if args.execute else 'DRY RUN'}\n")

    src_campaign = next(c for c in api.get_campaigns(src["ad_account_id"], page_size=100).get("list", [])
                         if c["id"] == src["campaign_id"])
    src_adset = next(a for a in api.get_ad_sets(src["ad_account_id"], campaign_ids=[src["campaign_id"]],
                                                 page_size=100).get("list", [])
                      if a["id"] == src_adset_id)
    creative_type = "IMAGE" if args.media_type == "image" else "VIDEO"
    src_ads = api.get_ads(src["ad_account_id"], ad_set_ids=[src_adset_id], page_size=500).get("list", [])
    src_ad = next(a for a in src_ads
                  if a.get("onlineStatus") == "ACTIVE" and a["creative"]["type"] == creative_type)
    content = src_ad["creative"]["content"]

    campaign_name = f"{args.to_account} | CONV | DIRECT"
    adset_name = src_adset["name"]
    campaign_status = src_campaign["status"] if args.as_is else "OFF"
    adset_status = src_adset["status"] if args.as_is else "OFF"
    ad_status = src_ad["status"] if args.as_is else "OFF"

    print(f"Campaign: '{campaign_name}'  objective={src_campaign['objective']}  status={campaign_status}")
    if args.as_is:
        print(f"Ad set:   '{adset_name}'  budget={src_adset['budget']/100:.2f}/{src_adset['budgetType']}  "
              f"bidType={src_adset['bidType']} (cloned as-is)  trackingId={src_adset.get('trackingId')} "
              f"(cloned as-is -- reports to {args.from_account}'s own conversion event until repointed)  "
              f"status={adset_status}")
    else:
        print(f"Ad set:   '{adset_name}'  budget={src_adset['budget']/100:.2f}/{src_adset['budgetType']}  "
              f"bidType=CPC (placeholder, ${PLACEHOLDER_BID_RATE_CENTS/100:.2f})  status={adset_status}  "
              f"(source used {src_adset['bidType']} + trackingId {src_adset.get('trackingId')} -- "
              f"not cloned, {args.to_account} has no conversion event yet)")

    print(f"\nDownloading source asset...")
    media_local = download(content["assetUrl"], Path(content["assetUrl"]).suffix or ".png")
    logo_local = download(content["logoUrl"], Path(content["logoUrl"]).suffix or ".jpg") if content.get("logoUrl") else None

    print("Generating fresh ad copy (Gemini)...")
    copy = custom_copy.generate_ad_copy(str(media_local), vertical=args.vertical,
                                         brand_name=content["brandName"], is_video=(args.media_type == "video"))
    h = hashlib.sha256(media_local.read_bytes()).hexdigest()
    ad_name = f"{'VID' if args.media_type == 'video' else 'IMG'} {h[:8]}"

    click_through_url = content.get("clickThroughUrl") if args.as_is else None

    print(f"Ad: '{ad_name}'  brandName={content['brandName']!r}  callToAction={content['callToAction']!r}")
    print(f"    Title: {copy['title']}")
    print(f"    Description: {copy['description']}")
    print(f"    clickThroughUrl: {click_through_url or f'(none -- {args.to_account} has no destination URL yet)'}")
    print(f"    status={ad_status}")

    if not args.execute:
        print("\n[DRY RUN] no campaign/ad set/ad created.")
        return

    print("\nCreating campaign...")
    new_campaign = api.create_campaign(dst["ad_account_id"], name=campaign_name,
                                        objective=src_campaign["objective"], status=campaign_status)
    print(f"  campaign id: {new_campaign['id']}")

    print("Creating ad set...")
    if args.as_is:
        new_adset = api.create_ad_set(
            campaign_id=new_campaign["id"], name=adset_name,
            budget_type=src_adset["budgetType"], budget=src_adset["budget"],
            start_time=src_adset["startTime"], end_time=src_adset["endTime"],
            bid_type=src_adset["bidType"], bid_rate=src_adset.get("bidRate"),
            tracking_id=src_adset.get("trackingId"), roas=src_adset.get("roas"),
            delivery_rate=src_adset.get("deliveryRate"), targeting=src_adset.get("targeting"),
            platforms=src_adset.get("platforms"), frequency_caps=src_adset.get("frequencyCaps"),
            status=adset_status,
        )
    else:
        new_adset = api.create_ad_set(
            campaign_id=new_campaign["id"], name=adset_name,
            budget_type=src_adset["budgetType"], budget=src_adset["budget"],
            start_time=src_adset["startTime"], end_time=src_adset["endTime"],
            bid_type="CPC", bid_rate=PLACEHOLDER_BID_RATE_CENTS,
            delivery_rate=src_adset.get("deliveryRate"), targeting=src_adset.get("targeting"),
            platforms=src_adset.get("platforms"), frequency_caps=src_adset.get("frequencyCaps"),
            status=adset_status,
        )
    print(f"  ad set id: {new_adset['id']}")

    print("Uploading media asset...")
    asset = api.upload_asset(dst["ad_account_id"], str(media_local))
    creative = {
        "type": creative_type,
        "headline": copy["title"],
        "assetUrl": asset["assetUrl"],
        "description": copy["description"],
        "callToAction": content["callToAction"],
        "brandName": content["brandName"],
    }
    if click_through_url:
        creative["clickThroughUrl"] = click_through_url
    if logo_local:
        print("Uploading logo asset...")
        logo_asset = api.upload_asset(dst["ad_account_id"], str(logo_local))
        creative["logoUrl"] = logo_asset["assetUrl"]

    print("Creating ad...")
    new_ad = api.create_ad(new_adset["id"], name=ad_name, creative=creative, status=ad_status)
    print(f"  ad id: {new_ad['id']} (status: {new_ad['onlineStatus']})")

    wrote_campaign = config.set_field_if_unset(args.to_account, "campaign_id", new_campaign["id"])
    wrote_adset = not config.get_adset_ids(args.to_account)
    if wrote_adset:
        config.append_adset_id(args.to_account, new_adset["id"])
    print(f"\naccounts.json: campaign_id {'registered' if wrote_campaign else 'left untouched (already set)'}, "
          f"adset_ids {'registered' if wrote_adset else 'left untouched (already has entries)'}")

    if args.as_is:
        print(f"\nDONE. Cloned as-is from {args.from_account} -- this ad set/ad currently reports conversions "
              f"and sends clicks to {args.from_account}'s own tracking event/landing page, not one of "
              f"{args.to_account}'s own. Repoint trackingId/clickThroughUrl via Update Ad Set / Update Ad "
              f"once {args.to_account} has its own conversion event + destination URL.")
    else:
        print(f"\nDONE. Everything created OFF/paused -- register {args.to_account}'s own conversion event and "
              f"destination URL, then use Update Ad Set / Update Ad to wire up trackingId/bidType/clickThroughUrl "
              f"and flip to ON when ready to go live.")


if __name__ == "__main__":
    main()
