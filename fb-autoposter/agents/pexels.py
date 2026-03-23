"""
pexels.py — Fetches a relevant image from the Pexels API for a given search query.

Uses Claude Vision to score candidate images and pick the best one.
Downloads the selected image to media/temp/ and returns metadata.

Returns:
    {
        "image_url":    str,   # direct URL to the image
        "image_path":   str,   # local path to downloaded file
        "photographer": str,   # photographer name for attribution
        "pexels_url":   str,   # page URL on Pexels
    }
"""

import base64
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL, PEXELS_API_KEY

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_HERE = Path(__file__).resolve().parent.parent
TEMP_DIR = _HERE / "media" / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


def _search_pexels(query: str, per_page: int = 10) -> list:
    """Call the Pexels search endpoint; return list of photo dicts."""
    params = urllib.parse.urlencode(
        {"query": query, "per_page": per_page, "orientation": "landscape"}
    )
    url = f"{PEXELS_SEARCH_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": PEXELS_API_KEY,
            "User-Agent": "curl/7.88.1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()).get("photos", [])


def _score_photo(photo: dict, context: str = "") -> float:
    """
    Download a photo and score it with Claude Vision on three criteria.
    context: the post copy (or topic) this image should complement.
    Returns average score 0–10, or 0.0 on failure.
    """
    url = photo["src"].get("medium") or photo["src"].get("large") or photo["src"]["original"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fb-autoposter/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = base64.standard_b64encode(r.read()).decode()
    except Exception as exc:
        log.warning("Could not download photo %s for scoring: %s", photo["id"], exc)
        return 0.0

    relevance_criterion = (
        f"relevance — how well this photo complements a post that says: "
        f"\"{context[:400]}\""
        if context
        else "relevance — bathroom or home maintenance subject"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Score this photo for use in a home renovation social media post.\n"
                            "Prefer: real bathroom/home context, good natural lighting, "
                            "high quality, lifestyle feel.\n"
                            "Avoid: overly staged stock look, visible watermarks, "
                            "irrelevant subjects.\n\n"
                            "Score 0–10 on each criterion:\n"
                            f"  {relevance_criterion}\n"
                            "  quality         — sharpness, lighting, composition\n"
                            "  lifestyle_feel  — looks real, not too corporate/staged\n\n"
                            "Respond ONLY with valid JSON, no extra text:\n"
                            '{"relevance":<float>,"quality":<float>,"lifestyle_feel":<float>}'
                        ),
                    },
                ],
            }
        ],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

    try:
        scores = json.loads(raw)
        return round(sum(scores.values()) / len(scores), 2)
    except (json.JSONDecodeError, ZeroDivisionError):
        return 0.0


def _pick_best_photo(photos: list, context: str = "") -> dict:
    """Score all photos with Claude Vision; return the highest-scoring one."""
    scored = []
    for photo in photos:
        try:
            score = _score_photo(photo, context)
            log.info("  Photo %s — score %.1f", photo["id"], score)
            scored.append((photo, score))
        except Exception as exc:
            log.warning("  Could not score photo %s: %s", photo["id"], exc)

    if not scored:
        # Fallback: highest resolution by width × height
        log.warning("No photos scored — falling back to highest resolution")
        return max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0))

    return max(scored, key=lambda t: t[1])[0]


def _download_photo(photo: dict, slug: str) -> Path:
    """Download the large version of a photo to TEMP_DIR."""
    url = (
        photo["src"].get("large2x")
        or photo["src"].get("large")
        or photo["src"]["original"]
    )
    ext = url.split("?")[0].rsplit(".", 1)[-1] or "jpg"
    dest = TEMP_DIR / f"pexels_{slug}.{ext}"

    req = urllib.request.Request(url, headers={"User-Agent": "fb-autoposter/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())

    log.info("Downloaded → %s", dest)
    return dest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_image(search_query: str, context: str = "") -> dict:
    """
    Find, score, and download the best Pexels image for a search query.

    context: optional post copy text — passed to Claude Vision so it scores
             photos for relevance to the actual post content, not just the topic.

    Falls back to a broadened query (last word stripped) if no results.

    Returns:
        {
            "image_url":    str,
            "image_path":   str,
            "photographer": str,
            "pexels_url":   str,
        }
    """
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY is not set in .env / config.py")

    slug = _slugify(search_query)
    photos = _search_pexels(search_query)

    if not photos:
        shortened = " ".join(search_query.split()[:-1])
        if shortened:
            log.warning("No results for %r — retrying with %r", search_query, shortened)
            photos = _search_pexels(shortened)

    if not photos:
        raise ValueError(f"No Pexels results found for query: {search_query!r}")

    best = _pick_best_photo(photos, context)
    local_path = _download_photo(best, slug)

    return {
        "image_url": best["src"].get("large2x") or best["src"]["original"],
        "image_path": str(local_path),
        "photographer": best["photographer"],
        "pexels_url": best["url"],
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = fetch_image("shower tile bathroom renovation")
    print("\n=== pexels result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
