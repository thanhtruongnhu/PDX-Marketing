"""
test_discord.py — Manual integration test for the Discord approval flow.

Sends a dummy Branch A (project story) and dummy Branch B (maintenance tip)
draft to your Discord channel, waits for your replies, and prints the outcome
to the terminal.

Usage:
    cd fb-autoposter
    python3 test_discord.py

How to test the revision loop:
    1. Reply "reject [notes]" up to 3 times — confirm revision prompts appear
    2. After the 3rd reject you should see the max-revisions override gate
    3. Reply "override [your custom copy]" / "approve" / "discard"

No real API calls are made — dummy post contexts are hardcoded below.
Do not wire into main.py (that happens in Phase 6).
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta

# ── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Import bot helpers ───────────────────────────────────────────────────────

try:
    from discord_bot import new_post_entry, run_approval_flow
except ImportError as exc:
    sys.exit(f"ERROR: Could not import discord_bot.py — {exc}")

# ── Dummy data ───────────────────────────────────────────────────────────────

# ISO-8601 times roughly 2 days out
_NOW   = datetime.now()
_SCHED_A = (_NOW + timedelta(days=2,  hours=10)).isoformat(timespec="minutes")
_SCHED_B = (_NOW + timedelta(days=2,  hours=14)).isoformat(timespec="minutes")

# Branch A — project story (images variant, no real image files)
DUMMY_BRANCH_A_POST_CONTEXT = {
    "copy": (
        "Before + after bathroom magic in Mississauga! 🛁✨\n\n"
        "This master ensuite went from cramped and dated to a full spa retreat. "
        "We installed a walk-in shower with white subway tile, a frameless glass door, "
        "double quartz-top vanity, oil-rubbed bronze fixtures, and heated floors.\n\n"
        "The homeowners said it now feels like checking into a hotel every morning. "
        "That's exactly what we're here for. 💪\n\n"
        "Thinking about a bathroom renovation? DM us or drop a comment below — "
        "free in-home consults across the GTA.\n\n"
        "PDX Remodelling Solutions - 437 238 8992"
    ),
    "hashtags": [
        "#BathroomRenovation", "#GTA", "#Mississauga",
        "#BeforeAndAfter", "#PDXRemodelingSolutions",
        "#HomeRenovation", "#BathroomRemodel",
    ],
    "project_name": "Mississauga Master Ensuite",
    "media_type":   "images",
}

DUMMY_BRANCH_A_MEDIA_CONTEXT = {
    "type":            "images",
    "before_image":    None,    # no real file — bot will skip attachment gracefully
    "after_image":     None,
    "video_path":      None,
    "content_context": (
        "Before: small pink-tiled tub/shower combo, single pedestal sink, "
        "low lighting. After: walk-in frameless glass shower with white subway "
        "tile, double floating vanity with quartz top, oil-rubbed bronze fixtures, "
        "heated herringbone floor tile."
    ),
    "project_name": "Mississauga Master Ensuite",
    "project_dir":  None,   # no real project dir (remedia not available in test)
}

# Branch B — maintenance tip
DUMMY_BRANCH_B_POST_CONTEXT = {
    "copy": (
        "Here in the GTA, winter freeze-thaw cycles are brutal on bathroom caulk. "
        "If you see cracking or discolouration around your tub or shower, it's time "
        "to re-seal before moisture sneaks in behind the wall.\n\n"
        "Step 1: Score the old caulk with a utility knife and peel it out fully.\n"
        "Step 2: Clean the joint with rubbing alcohol — let it dry completely.\n"
        "Step 3: Apply a fresh bead of 100% silicone caulk in one smooth pass.\n"
        "Step 4: Wet your finger and smooth the bead, then leave it 24 hours before "
        "getting it wet.\n\n"
        "Total time: about 30 minutes. Total cost: ~$8. "
        "Beats a $4,000 water-damage repair any day. 💧\n\n"
        "Questions? Drop them in the comments or DM us!\n\n"
        "PDX Remodelling Solutions - 437 238 8992\n"
        "📷 Photo by Max Vakhtbovycn via Pexels"
    ),
    "hashtags": [
        "#BathroomTips", "#GTA", "#HomeMaintenanceTips",
        "#GroutAndCaulk", "#PDXRemodelingSolutions",
        "#TorontoHomes", "#DIYHomeRepair",
    ],
    "topic":      "When to replace caulk around a bathtub or shower",
    "image_path": None,   # no real Pexels image in test mode
}

DUMMY_BRANCH_B_MEDIA_CONTEXT = {
    "topic":      "When to replace caulk around a bathtub or shower",
    "post_angle": "freeze-thaw cycles in GTA make caulk maintenance critical",
}

# Validator breakdown scores (simulated — 8/10 across the board)
DUMMY_SCORE_BREAKDOWN = {
    "hook_strength": 8.0,
    "clarity":       8.5,
    "cta":           7.8,
    "tone_match":    8.2,
    "length":        7.9,
}
DUMMY_SCORE = round(sum(DUMMY_SCORE_BREAKDOWN.values()) / len(DUMMY_SCORE_BREAKDOWN), 2)


# ── Build entries ────────────────────────────────────────────────────────────

def build_test_entries() -> list:
    entry_a = new_post_entry(
        branch          = "A",
        post_context    = DUMMY_BRANCH_A_POST_CONTEXT,
        media_context   = DUMMY_BRANCH_A_MEDIA_CONTEXT,
        scheduled_time  = _SCHED_A,
        last_score      = DUMMY_SCORE,
        score_breakdown = DUMMY_SCORE_BREAKDOWN,
    )
    entry_b = new_post_entry(
        branch          = "B",
        post_context    = DUMMY_BRANCH_B_POST_CONTEXT,
        media_context   = DUMMY_BRANCH_B_MEDIA_CONTEXT,
        scheduled_time  = _SCHED_B,
        last_score      = DUMMY_SCORE,
        score_breakdown = DUMMY_SCORE_BREAKDOWN,
    )
    return [entry_a, entry_b]


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log.info("Building test post entries (no API calls — dummy data only)")
    entries = build_test_entries()

    log.info("Branch A post_id: %s", entries[0]["post_id"])
    log.info("Branch B post_id: %s", entries[1]["post_id"])

    log.info(
        "Starting Discord approval flow — "
        "go to your Discord channel and reply to the drafts."
    )
    print()
    print("=" * 60)
    print("  DISCORD APPROVAL TEST")
    print("  Reply to each draft message with:")
    print("    approve")
    print("    reject [your notes]")
    print("    remedia [your notes]  (Branch A only)")
    print("  After 3 rejects: approve / discard / override [copy]")
    print("=" * 60)
    print()

    approved = await run_approval_flow(entries)

    print()
    print("=" * 60)
    print(f"  RESULT: {len(approved)} post(s) approved")
    for i, ctx in enumerate(approved, 1):
        print(f"\n  [{i}] {ctx.get('project_name') or ctx.get('topic', 'Unknown')}")
        snippet = ctx.get("copy", "")[:120].replace("\n", " ")
        print(f"      Copy preview: {snippet}…")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Test interrupted by user")
    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        sys.exit(1)
