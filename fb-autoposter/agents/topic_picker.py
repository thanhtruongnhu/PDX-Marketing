"""
topic_picker.py — Picks 3 bathroom/home maintenance topics per weekly run.

- Uses Claude to generate topic suggestions relevant to Portland, OR homeowners
- Reads topic_history.json to avoid repeating topics used in the last 8 weeks
- Calls pexels.py once per topic to fetch a supporting image
- Calls generate_maintenance_post() to produce ready-to-publish post dicts
- Writes used topics to topic_history.json
- Returns a list of 3 maintenance post_context dicts

Each returned dict:
    {
        "copy":         str,
        "hashtags":     list[str],
        "topic":        str,
        "image_path":   str,
        "photographer": str,
    }
"""

import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_HERE = Path(__file__).resolve().parent.parent
HISTORY_FILE = _HERE / "topic_history.json"
WEEKS_TO_AVOID = 8


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def _load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"history": []}


def _save_history(data: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(data, indent=2))


def _recent_topics(history: list) -> list:
    """Return topic strings used within the last WEEKS_TO_AVOID weeks."""
    cutoff = date.today() - timedelta(weeks=WEEKS_TO_AVOID)
    return [
        e["topic"]
        for e in history
        if date.fromisoformat(e["used_on"]) >= cutoff
    ]


# ---------------------------------------------------------------------------
# Claude topic generation
# ---------------------------------------------------------------------------


def _ask_claude_for_topics(avoid: list, n: int = 3) -> list:
    """
    Ask Claude to suggest n fresh maintenance topics.

    Each topic dict:
        {
            "topic":        str,   e.g. "How to reseal shower grout"
            "search_query": str,   e.g. "shower grout resealing bathroom"
            "post_angle":   str,   e.g. "prevention tip for homeowners"
        }
    """
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\n\nDo NOT suggest any of these recently used topics:\n"
            + "\n".join(f"- {t}" for t in avoid)
        )

    prompt = (
        f"Suggest {n} bathroom and home maintenance topics for weekly Facebook posts "
        "aimed at Toronto, ON (GTA) homeowners.\n\n"
        "Topics should be practical, specific, and actionable — covering areas such as:\n"
        "grout resealing, caulking replacement, bathroom ventilation, tile care, "
        "mould prevention, waterproofing, showerhead cleaning, exhaust fan maintenance, "
        "plumbing basics, moisture control, and seasonal home upkeep.\n\n"
        "GTA homeowners deal with harsh winters, freeze-thaw cycles, and humid summers — "
        "factor this in where it makes the tip more relevant.\n"
        + avoid_block
        + "\n\nFor each topic return exactly these three fields:\n"
        '  "topic":        short descriptive title (e.g. "How to reseal shower grout")\n'
        '  "search_query": 4–6 word Pexels image search query '
        '(e.g. "shower grout resealing bathroom tile")\n'
        '  "post_angle":   one sentence describing the post angle '
        '(e.g. "prevention tip for homeowners before winter")\n\n'
        f"Respond ONLY with a JSON array of exactly {n} objects, no extra text:\n"
        '[{"topic":"...","search_query":"...","post_angle":"..."},...]'
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

    return json.loads(raw)


def _record_topics(topics: list) -> None:
    """Append today's topics to topic_history.json."""
    data = _load_history()
    today = str(date.today())
    for t in topics:
        data["history"].append({"topic": t["topic"], "used_on": today})
    _save_history(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pick_topics(n: int = 3) -> list:
    """
    Select n maintenance topics, fetch a Pexels image per topic,
    generate a post for each, and return a list of post_context dicts.

    Each dict returned:
        {
            "copy":         str,
            "hashtags":     list[str],
            "topic":        str,
            "image_path":   str,
            "photographer": str,
        }
    """
    from agents.pexels import fetch_image
    from agents.generator import generate_maintenance_post

    history_data = _load_history()
    avoid = _recent_topics(history_data.get("history", []))
    log.info("Skipping %d recently used topics", len(avoid))

    topics = _ask_claude_for_topics(avoid, n=n)
    log.info("Topics selected: %s", [t["topic"] for t in topics])

    post_contexts = []
    for t in topics:
        # --- Fetch Pexels image ---
        log.info("Fetching Pexels image for: %s", t["search_query"])
        try:
            img = fetch_image(t["search_query"])
        except Exception as exc:
            log.warning(
                "Image fetch failed for %r: %s — using empty placeholder",
                t["topic"],
                exc,
            )
            img = {
                "image_url": "",
                "image_path": "",
                "photographer": "unknown",
                "pexels_url": "",
            }

        maintenance_context = {
            "topic": t["topic"],
            "post_angle": t["post_angle"],
            **img,
        }

        # --- Generate post ---
        log.info("Generating post for: %s", t["topic"])
        post_ctx = generate_maintenance_post(maintenance_context)
        post_contexts.append(post_ctx)

    _record_topics(topics)
    log.info("Saved %d topics to topic_history.json", len(topics))

    return post_contexts


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    results = pick_topics(n=3)

    print("\n" + "=" * 60)
    print("Branch B — Maintenance Post Drafts")
    print("=" * 60)

    for i, ctx in enumerate(results, 1):
        print(f"\n{'─' * 50}")
        print(f"Post {i}: {ctx['topic']}")
        print(f"{'─' * 50}")
        print(ctx["copy"])
        print(f"\nHashtags: {' '.join(ctx['hashtags'])}")
        print(f"Image:    {ctx['image_path'] or '(none)'}")
        print(f"Credit:   {ctx['photographer']}")
