"""Generates fresh Title/Description ad copy for a NewsBreak native ad by
having Gemini look at the actual image/video creative -- same role Meta's
custom-copy generation plays, adapted to NewsBreak's own constraints
(90-char cap on both fields, and both fields must include the literal
"{city}" token, which NewsBreak's ad server substitutes with the
viewer's actual city at serve time -- do not paraphrase or drop it).

Reuses video-analysis's own gemini_client.py (upload/analyze utility,
not any of its video-specific business logic) via the same
importlib-dynamic-load pattern meta-ads-automation's own custom_copy.py
already established, rather than reimplementing Gemini file upload +
polling from scratch.
"""
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

VIDEO_ANALYSIS_ROOT = Path(r"C:\Users\USER\video-analysis")
WINNERS_DIR = VIDEO_ANALYSIS_ROOT / "winners"
_VA_ALIAS = "_video_analysis_pkg_for_newsbreak"

TITLE_MAX_CHARS = 90
DESCRIPTION_MAX_CHARS = 90

TRANSCRIPT_SECTION_RE = re.compile(
    r"###\s*FULL TRANSCRIPT\s*\n(.*?)(?:\n---|\Z)", re.DOTALL | re.IGNORECASE,
)

# Shared rules block -- identical in both prompt variants below, just
# templated in once rather than duplicated.
_RULES = '''Rules (title and description fields only):
- Both MUST include the literal text "{{city}}" exactly as written (four characters: curly-brace, c-i-t-y, curly-brace) -- this is a template token NewsBreak's ad server replaces with the viewer's real city at serve time. Do NOT replace it, translate it, or describe what it means -- just include the literal token.
- Vague on mechanism: say "real estimate," never "real number(s)." Never claim an instant on-site/immediate estimate. Never say the offer "connects you with a local contractor" -- keep it vague on how the estimate is delivered.
- No fake-discount or specific-promotional-price framing (e.g. avoid a crossed-out price vs. a specific lower price presented as a deal).
- No guaranteed-outcome claims.

Respond with ONLY a JSON object, no markdown fences, no other text:
{{"title": "...", "description": "...", "url_slug_title": "..."}}
'''

COPY_PROMPT_TEMPLATE = '''You are writing native ad copy for a NewsBreak ad promoting {vertical} lead-gen (brand name: "{brand_name}"). Look at the attached creative (image or video) and write:

1. A "title" (max {title_max} characters, hard limit) -- a punchy, scroll-stopping hook relevant to what's shown in the creative.
2. A "description" (max {desc_max} characters, hard limit) -- one supporting sentence reinforcing the hook.
3. A "url_slug_title" -- same tone/style as the title above, just longer -- exactly 8-10 words, congruent with the creative's core angle/hook. Plain title-case words only, no punctuation, and do NOT include the "{{city}}" token here -- this becomes a URL tracking slug, not displayed ad copy.

''' + _RULES

# Text-only variant -- used when this exact video already has a transcript
# sitting in video-analysis's own winners/ folder (it was launched to Meta
# first via meta-launch-tier1/meta-launch-direct, which always generates
# one). Reusing that transcript costs zero video upload/multimodal Gemini
# calls -- gc.analyze_text() instead of gc.analyze_video()/analyze_image() --
# added 2026-09-02 at the user's explicit request specifically to avoid
# re-analyzing a video Gemini has already fully transcribed once.
COPY_PROMPT_TEMPLATE_FROM_TRANSCRIPT = '''You are writing native ad copy for a NewsBreak ad promoting {vertical} lead-gen (brand name: "{brand_name}"). Below is the full transcript (or, for an image ad, a description) of the creative. Based on it, write:

1. A "title" (max {title_max} characters, hard limit) -- a punchy, scroll-stopping hook relevant to the creative's actual content below.
2. A "description" (max {desc_max} characters, hard limit) -- one supporting sentence reinforcing the hook.
3. A "url_slug_title" -- same tone/style as the title above, just longer -- exactly 8-10 words, congruent with the creative's core angle/hook. Plain title-case words only, no punctuation, and do NOT include the "{{city}}" token here -- this becomes a URL tracking slug, not displayed ad copy.

''' + _RULES + '''
Creative transcript/description:
"""
{transcript}
"""
'''


def _load_video_analysis_package():
    if _VA_ALIAS in sys.modules:
        return sys.modules[_VA_ALIAS]

    load_dotenv(dotenv_path=VIDEO_ANALYSIS_ROOT / ".env")

    pkg_init = VIDEO_ANALYSIS_ROOT / "src" / "__init__.py"
    if not pkg_init.is_file():
        raise RuntimeError(
            f"video-analysis project not found at {VIDEO_ANALYSIS_ROOT} -- custom copy "
            f"generation needs its gemini_client.py for file upload/analysis."
        )
    spec = importlib.util.spec_from_file_location(
        _VA_ALIAS, pkg_init, submodule_search_locations=[str(VIDEO_ANALYSIS_ROOT / "src")],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_VA_ALIAS] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def _gemini_client():
    _load_video_analysis_package()
    return importlib.import_module(f"{_VA_ALIAS}.gemini_client")


def _strip_json_fences(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def _local_transcript(media_path: str) -> str | None:
    """This exact file's transcript (or image description), IF it was
    already launched to Meta via meta-launch-tier1/meta-launch-direct --
    both always generate a video-analysis/winners/<stem>.md report before
    launching, keyed by the file's own stem (filename without extension),
    which matches across projects since these are literally the same media
    files. Returns None (not an error) if no such report exists, so the
    caller can fall back to a fresh Gemini upload+analyze -- this is a
    cache hit/miss, not a required precondition.
    """
    report_path = WINNERS_DIR / f"{Path(media_path).stem}.md"
    if not report_path.is_file():
        return None
    report_text = report_path.read_text(encoding="utf-8", errors="replace")
    match = TRANSCRIPT_SECTION_RE.search(report_text)
    return match.group(1).strip() if match else None


def slugify(title: str) -> str:
    """Turns the 8-10 word url_slug_title into a URL-safe query-param
    value: strip punctuation, capitalize each word, join with '+'.
    Same convention as meta-ads-automation's own custom_copy.slugify(),
    so NewsBreak's dh1 tracking param matches Meta's exactly.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9\s+]", "", title).strip()
    words = re.split(r"\s+", cleaned)
    return "+".join(w[:1].upper() + w[1:] for w in words if w)


def generate_ad_copy(media_path: str, vertical: str, brand_name: str, is_video: bool) -> dict:
    """Returns {"title": str, "description": str}, both already validated
    against the length caps and the required "{city}" token. Raises if
    Gemini's output doesn't comply after one retry.

    Reuses this exact file's already-generated video-analysis transcript
    (see _local_transcript()) when one exists -- a cheap text-only Gemini
    call instead of re-uploading and re-analyzing the raw media, since
    every video/image launched here has typically already gone to Meta
    first (via meta-launch-tier1/meta-launch-direct), which always
    produces that transcript. Falls back to the original upload+analyze
    flow when no local transcript is found (e.g. NewsBreak-only creative
    that was never launched to Meta).
    """
    gc = _gemini_client()
    transcript = _local_transcript(media_path)

    if transcript is not None:
        print(f"    (reusing local transcript from winners/{Path(media_path).stem}.md -- "
              f"no video/image upload needed)")
        prompt = COPY_PROMPT_TEMPLATE_FROM_TRANSCRIPT.format(
            vertical=vertical, brand_name=brand_name,
            title_max=TITLE_MAX_CHARS, desc_max=DESCRIPTION_MAX_CHARS, transcript=transcript,
        )
        analyze_call = lambda: gc.analyze_text(prompt)  # noqa: E731
    else:
        prompt = COPY_PROMPT_TEMPLATE.format(
            vertical=vertical, brand_name=brand_name,
            title_max=TITLE_MAX_CHARS, desc_max=DESCRIPTION_MAX_CHARS,
        )
        media_file = gc.upload_video(media_path)  # handles both images and videos, see gemini_client.py
        analyze = gc.analyze_video if is_video else gc.analyze_image
        analyze_call = lambda: analyze(media_file, prompt)  # noqa: E731

    for attempt in range(2):
        raw = analyze_call()
        try:
            parsed = json.loads(_strip_json_fences(raw))
            title = parsed["title"].strip()
            description = parsed["description"].strip()
            url_slug_title = parsed["url_slug_title"].strip()
        except (json.JSONDecodeError, KeyError) as exc:
            if attempt == 0:
                continue
            raise RuntimeError(f"Gemini did not return valid copy JSON: {raw!r}") from exc

        problems = []
        if len(title) > TITLE_MAX_CHARS:
            problems.append(f"title is {len(title)} chars (max {TITLE_MAX_CHARS})")
        if len(description) > DESCRIPTION_MAX_CHARS:
            problems.append(f"description is {len(description)} chars (max {DESCRIPTION_MAX_CHARS})")
        if "{city}" not in title:
            problems.append("title missing literal {city} token")
        if "{city}" not in description:
            problems.append("description missing literal {city} token")

        if not problems:
            return {"title": title, "description": description, "url_slug_title": url_slug_title}
        if attempt == 1:
            raise RuntimeError(f"Gemini copy failed validation twice: {problems}")

    raise RuntimeError("unreachable")
