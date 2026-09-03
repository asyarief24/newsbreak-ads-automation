"""Creates new NewsBreak ads in an existing ad set by cloning an existing
ad's non-creative settings (brandName, logoUrl, clickThroughUrl,
callToAction) per media type, swapping in a new image/video, and
generating fresh Title/Description copy via Gemini (custom_copy.py).

Dry-run by default -- prints what would happen (including the real
generated copy, so it can be reviewed) without uploading anything or
creating real ads. Pass --execute to actually do it.

Usage:
    # Dry run -- short account key resolves ad-account-id/ad-set-id from accounts.json
    python scripts/launch_ads.py --account RF \
        --images-dir "C:\\Users\\USER\\meta-ads-automation\\images\\RF\\_launched" \
        --videos-dir "C:\\Users\\USER\\meta-ads-automation\\videos\\RF\\_launched" \
        --vertical roofing --test 2

    # Real run (after reviewing the dry run)
    ... same args ... --execute

    # Raw IDs still work too (e.g. for an account not yet in accounts.json)
    python scripts/launch_ads.py --ad-account-id 2084570850379935745 \
        --ad-set-id 2091745516889493505 --images-dir ... --execute
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
import custom_copy  # noqa: E402
import newsbreak_api as api  # noqa: E402

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
REGISTRY_DIR = Path(__file__).resolve().parent.parent / "launched_registry"
# NewsBreak caps an ad set at 50 ads (confirmed live, error code 41108) --
# but the cap isn't enforced exactly at 50 (RF/HVAC both got real ads
# through up to 57/59 total before the error actually fired), so rotate
# well before the documented limit rather than trusting that slack.
AD_SET_CAP = 50
AD_SET_ROTATE_AT = 45


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_registry(ad_account_id: str) -> dict:
    """Keyed by ad_account_id (added 2026-09-03, migrated from per-ad_set_id)
    -- a video/image already launched anywhere in this account should never
    relaunch just because it's now targeting a different (overflow) ad set.
    Old per-ad-set files are left in place, just unused; see git history if
    an account's pre-migration history needs re-merging."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    path = REGISTRY_DIR / f"{ad_account_id}.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {}


def save_registry(ad_account_id: str, registry: dict) -> None:
    path = REGISTRY_DIR / f"{ad_account_id}.json"
    path.write_text(json.dumps(registry, indent=2))


def next_adset_name(current_name: str) -> str:
    """"Ad Set 1 - Max Conversion" -> "Ad Set 2 - Max Conversion", etc.
    Falls back to appending "(overflow)" if the name doesn't match this
    project's own "Ad Set N" naming convention (e.g. a manually renamed
    ad set)."""
    match = re.search(r"Ad Set (\d+)", current_name)
    if not match:
        return f"{current_name} (overflow)"
    n = int(match.group(1)) + 1
    return current_name[:match.start()] + f"Ad Set {n}" + current_name[match.end():]


def create_overflow_adset(ad_account_id: str, current_adset_id: str, account_key: str | None) -> str:
    """Clones current_adset_id's exact live settings into a new ad set
    under the same campaign -- added 2026-09-03, at the user's explicit
    request, after this same manual procedure (documented in this skill's
    SKILL.md) had to be repeated by hand for both RF and HVAC. Registers
    the new ad set in accounts.json (config.append_adset_id()) when
    account_key is known, so a later --account run picks it up as the
    current target automatically; a raw --ad-account-id/--ad-set-id
    invocation just gets the new ID printed to note manually.
    """
    adsets = api.get_ad_sets(ad_account_id, page_size=100).get("list", [])
    current = next((a for a in adsets if a["id"] == current_adset_id), None)
    if not current:
        raise RuntimeError(f"Could not fetch current ad set {current_adset_id}'s settings to clone.")

    new_name = next_adset_name(current["name"])
    print(f"  Ad set {current_adset_id} is nearing NewsBreak's {AD_SET_CAP}-ad cap -- "
          f"creating overflow ad set '{new_name}' cloned from its settings...")
    new_adset = api.create_ad_set(
        campaign_id=current["campaignId"], name=new_name,
        budget_type=current["budgetType"], budget=current["budget"],
        start_time=current["startTime"], end_time=current["endTime"],
        bid_type=current["bidType"], bid_rate=current.get("bidRate"),
        tracking_id=current.get("trackingId"), roas=current.get("roas"),
        delivery_rate=current.get("deliveryRate"), targeting=current.get("targeting"),
        platforms=current.get("platforms"), frequency_caps=current.get("frequencyCaps"),
        status=current["status"],
    )
    print(f"  New overflow ad set: {new_adset['id']}")
    if account_key:
        config.append_adset_id(account_key, new_adset["id"])
        print(f"  Registered in accounts.json under '{account_key}'.")
    else:
        print(f"  NOTE: no --account key given, so this wasn't recorded in accounts.json -- "
              f"note ad set {new_adset['id']} manually if you'll need it again.")
    return new_adset["id"]


def find_template_ad(ads: list[dict], creative_type: str) -> dict | None:
    for ad in ads:
        if ad.get("onlineStatus") == "ACTIVE" and ad.get("creative", {}).get("type") == creative_type:
            return ad
    return None


def resolve_clickthrough_url_from_event(ad_account_id: str, ad_set_id: str) -> str | None:
    """Looks up this ad set's own trackingId and returns that Event's
    destination `url` -- used as the base clickThroughUrl when bootstrapping
    an ad set that has no ACTIVE ad of its own to clone clickThroughUrl
    from yet (see --template-from-account)."""
    adsets = api.get_ad_sets(ad_account_id, page_size=100).get("list", [])
    adset = next((a for a in adsets if a["id"] == ad_set_id), None)
    tracking_id = adset.get("trackingId") if adset else None
    if not tracking_id:
        return None
    events = api.get_events(ad_account_id)
    event = next((e for e in events if e["id"] == tracking_id), None)
    return event["url"] if event else None


def find_foreign_template(account_key: str, creative_type: str) -> dict:
    """Bootstrap fallback for an ad set with no ACTIVE ad of its own yet
    (e.g. brand new, or its one ad is still PENDING review): clones
    brandName/logoUrl/callToAction from another account's own ACTIVE ad of
    the same creative type instead. clickThroughUrl is NOT taken from this
    -- it belongs to the foreign account, not the one we're launching into
    -- see resolve_clickthrough_url_from_event() for the real source.

    Searches ALL of that account's ad sets (config.get_adset_ids()), not
    just its current one -- an account with an overflow ad set (2026-09-03+)
    may have its only ACTIVE template of a given creative_type sitting in
    an earlier ad set, not necessarily the one currently being filled.
    """
    acc = config.get_account(account_key)
    adset_ids = config.get_adset_ids(account_key)
    if not adset_ids:
        raise SystemExit(f"--template-from-account '{account_key}' has no adset_ids configured yet.")
    ads = api.get_ads(acc["ad_account_id"], ad_set_ids=adset_ids, page_size=500).get("list", [])
    template = find_template_ad(ads, creative_type)
    if not template:
        raise SystemExit(f"--template-from-account '{account_key}' has no ACTIVE {creative_type} ad either.")
    return template


def build_click_through_url(template_content: dict, url_slug_title: str) -> str | None:
    """Template's clickThroughUrl with `?dh1=<slug>` appended (`&dh1=`
    instead if it already has a query string) -- same convention as
    meta-ads-automation's own build_custom_url()/slugify(), so this
    account's NewsBreak dh1 tracking param matches Meta's exactly.

    Strips any existing dh1 param from the template first -- once a
    template ad's own clickThroughUrl already carries a dh1 (e.g. it was
    itself created by this same script), naively appending would stack a
    second dh1= instead of replacing it (confirmed live, 2026-08-31,
    launching HVAC's 2nd ad off its own just-approved 1st ad as template).
    """
    base_link = template_content.get("clickThroughUrl")
    if not base_link:
        return None
    parsed = urllib.parse.urlsplit(base_link)
    query_pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != "dh1"]
    base_link = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query_pairs)))
    slug = custom_copy.slugify(url_slug_title)
    if not slug:
        return base_link
    separator = "&" if "?" in base_link else "?"
    return f"{base_link}{separator}dh1={slug}"


def extract_video_cover(video_path: Path) -> Path:
    """First-frame thumbnail via ffmpeg, for the video's coverUrl."""
    out_path = Path(tempfile.gettempdir()) / f"{video_path.stem}_cover.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


def collect_media_files(images_dir: str | None, videos_dir: str | None) -> list[tuple[Path, bool]]:
    """Returns [(path, is_video), ...]."""
    files = []
    if images_dir:
        for p in sorted(Path(images_dir).iterdir()):
            if p.suffix.lower() in IMAGE_EXTENSIONS:
                files.append((p, False))
    if videos_dir:
        for p in sorted(Path(videos_dir).iterdir()):
            if p.suffix.lower() in VIDEO_EXTENSIONS:
                files.append((p, True))
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="Short account key from accounts.json (e.g. RF) -- "
                                           "resolves --ad-account-id and --ad-set-id automatically.")
    parser.add_argument("--ad-account-id")
    parser.add_argument("--ad-set-id")
    parser.add_argument("--images-dir", help="Folder of image files to launch.")
    parser.add_argument("--videos-dir", help="Folder of video files to launch.")
    parser.add_argument("--vertical", default="roofing", help="Used in the copy-generation prompt.")
    parser.add_argument("--template-from-account", help="Account key to clone brandName/logoUrl/callToAction "
                                                          "from when this ad set has no ACTIVE ad of its own "
                                                          "yet to use as a template (e.g. bootstrapping a new "
                                                          "account, or a fresh overflow ad set once one hits "
                                                          "NewsBreak's 50-ads-per-ad-set cap). clickThroughUrl "
                                                          "is reused as-is from the foreign ad when it's the SAME "
                                                          "ad account (just a different ad set); for a genuinely "
                                                          "different account it's instead resolved from this ad "
                                                          "set's own trackingId's event.")
    parser.add_argument("--test", type=int, nargs="?", const=1, default=None,
                         help="Only process the first N files (default 1 if flag given bare).")
    parser.add_argument("--execute", action="store_true", help="Actually upload and create ads.")
    args = parser.parse_args()

    if args.account:
        acc = config.get_account(args.account)
        ad_account_id = args.ad_account_id or acc["ad_account_id"]
        ad_set_id = args.ad_set_id or config.current_adset_id(args.account)
        all_known_adset_ids = config.get_adset_ids(args.account)
        if not ad_set_id:
            raise SystemExit(
                f"accounts.json entry for '{args.account}' has no adset_ids set, and "
                f"--ad-set-id wasn't passed either."
            )
    else:
        if not args.ad_account_id or not args.ad_set_id:
            raise SystemExit("Provide --account, or both --ad-account-id and --ad-set-id.")
        ad_account_id = args.ad_account_id
        ad_set_id = args.ad_set_id
        all_known_adset_ids = [ad_set_id]

    if not args.images_dir and not args.videos_dir:
        raise SystemExit("Provide --images-dir and/or --videos-dir.")

    media_files = collect_media_files(args.images_dir, args.videos_dir)
    if args.test:
        media_files = media_files[: args.test]

    registry = load_registry(ad_account_id)

    # Searches every ad set this account has ever had (not just the one
    # currently being filled) -- an account with an overflow ad set may
    # have its only ACTIVE template of a given creative_type sitting in an
    # earlier one. See find_foreign_template()'s identical reasoning.
    existing_ads = api.get_ads(ad_account_id, ad_set_ids=all_known_adset_ids, page_size=500).get("list", [])
    template_cache: dict[str, dict] = {}

    def get_template(creative_type: str) -> dict:
        if creative_type in template_cache:
            return template_cache[creative_type]
        template = find_template_ad(existing_ads, creative_type)
        if template is None:
            if not args.template_from_account:
                raise SystemExit(
                    f"No ACTIVE {creative_type} ad found in this ad set to use as a template, and "
                    f"--template-from-account wasn't given to bootstrap from another account's."
                )
            foreign_acc = config.get_account(args.template_from_account)
            foreign = find_foreign_template(args.template_from_account, creative_type)
            content = dict(foreign["creative"]["content"])
            if foreign_acc["ad_account_id"] == ad_account_id:
                # Same account, just a different ad set within it (e.g.
                # bootstrapping a fresh overflow ad set once the first hit
                # NewsBreak's 50-ads-per-ad-set cap) -- the foreign ad's own
                # clickThroughUrl is already correct and real for THIS
                # account, unlike the cross-account case below. Confirmed
                # live, 2026-09-02: the event-resolved URL for RF's own
                # trackingId pointed at a completely different domain
                # (home-improvements.co, the POSTBACK callback endpoint)
                # than what RF's real Ad Set 1 ads actually use as
                # clickThroughUrl (home-improvements.work, the real
                # user-facing landing page) -- these are NOT
                # interchangeable, so don't override here.
                print(f"  (bootstrapping {creative_type} template from --template-from-account "
                      f"'{args.template_from_account}' ad '{foreign['name']}' -- same account, "
                      f"reusing its own real clickThroughUrl as-is)")
            else:
                click_url = resolve_clickthrough_url_from_event(ad_account_id, ad_set_id)
                if not click_url:
                    raise SystemExit(
                        f"Bootstrapping from --template-from-account '{args.template_from_account}' needs this "
                        f"ad set's own trackingId to resolve a real clickThroughUrl, but none was found -- "
                        f"this ad set has no trackingId, or no matching Event."
                    )
                content["clickThroughUrl"] = click_url
                print(f"  (bootstrapping {creative_type} template from --template-from-account "
                      f"'{args.template_from_account}' ad '{foreign['name']}' -- clickThroughUrl resolved "
                      f"from this ad set's own trackingId's event instead)")
            template = {**foreign, "creative": {**foreign["creative"], "content": content}}
        template_cache[creative_type] = template
        return template

    print(f"Ad set: {ad_set_id}  |  Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Found {len(media_files)} media file(s) to process.\n")

    # Live-tracked count for auto-overflow (--execute only -- a dry run
    # never creates anything, so there's nothing to rotate away from).
    # Fetched once here, then updated in-memory after each real success --
    # avoids an extra API call per video just to recheck the count.
    current_adset_id = ad_set_id
    current_adset_count = (
        len(api.get_ads(ad_account_id, ad_set_ids=[ad_set_id], page_size=500).get("list", []))
        if args.execute else 0
    )

    succeeded = 0
    failures = []
    for media_path, is_video in media_files:
        print("=" * 70)
        print(media_path.name)
        print("=" * 70)

        h = file_hash(media_path)
        if h in registry:
            print(f"  Already launched (ad id {registry[h]['adId']}) -- skipping.\n")
            continue

        ad_name = f"{'VID' if is_video else 'IMG'} {h[:8]}"

        try:
            creative_type = "VIDEO" if is_video else "IMAGE"
            template = get_template(creative_type)
            template_content = template["creative"]["content"]

            print("  Generating custom copy (Gemini)...")
            copy = custom_copy.generate_ad_copy(
                str(media_path), vertical=args.vertical,
                brand_name=template_content["brandName"], is_video=is_video,
            )
            print(f"    Title: {copy['title']}")
            print(f"    Description: {copy['description']}")

            click_through_url = build_click_through_url(template_content, copy["url_slug_title"])
            print(f"    clickThroughUrl: {click_through_url}")

            cover_path = None
            if is_video:
                print("  Extracting cover thumbnail (ffmpeg)...")
                cover_path = extract_video_cover(media_path)

            if not args.execute:
                print(f"  [DRY RUN] would upload '{media_path.name}'"
                      + (f" + cover '{cover_path.name}'" if cover_path else ""))
                print(f"  [DRY RUN] would create {creative_type} ad named '{ad_name}' in ad set "
                      f"{ad_set_id}, cloning brandName/logoUrl/clickThroughUrl/callToAction "
                      f"from ad '{template['name']}'")
                print()
                continue

            print("  Uploading asset...")
            asset = api.upload_asset(ad_account_id, str(media_path))
            creative = {
                "type": creative_type,
                "headline": copy["title"],
                "assetUrl": asset["assetUrl"],
                "description": copy["description"],
                "callToAction": template_content["callToAction"],
                "brandName": template_content["brandName"],
                "clickThroughUrl": click_through_url,
                "logoUrl": template_content.get("logoUrl"),
            }
            if cover_path:
                print("  Uploading cover thumbnail...")
                cover_asset = api.upload_asset(ad_account_id, str(cover_path))
                creative["coverUrl"] = cover_asset["assetUrl"]

            if current_adset_count >= AD_SET_ROTATE_AT:
                current_adset_id = create_overflow_adset(ad_account_id, current_adset_id, args.account)
                current_adset_count = 0

            print("  Creating ad...")
            result = api.create_ad(current_adset_id, name=ad_name, creative=creative)
            print(f"  DONE. New ad id: {result['id']} (status: {result['onlineStatus']})")

            registry[h] = {"adId": result["id"], "sourcePath": str(media_path)}
            save_registry(ad_account_id, registry)
            current_adset_count += 1
            succeeded += 1
            print()
            # Small pause between ads -- extra defense against the undocumented
            # write-throttle confirmed live 2026-09-03 (see newsbreak_api.py's
            # RATE_LIMIT_MESSAGE_SUBSTRING), which fires under sustained
            # back-to-back /ad/create calls in a large batch. The API-layer
            # retry already recovers from it, but not paying for retries in
            # the first place is cheaper for a 60+ video batch.
            time.sleep(2)
        except Exception as e:  # noqa: BLE001 -- one video's failure (transient or
                                  # real) must not abort the rest of the batch --
                                  # confirmed live, 2026-09-03: a persistent
                                  # "Invalid operation" error on HVAC's first video
                                  # crashed the entire 66-video run twice before this
                                  # fix, leaving 65 untouched videos unprocessed.
            print(f"  FAILED: {e}\n")
            failures.append(media_path.name)

    print("=" * 70)
    print(f"{succeeded}/{len(media_files)} launched.")
    if failures:
        print(f"Failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
