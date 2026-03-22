"""
test_facebook.py — Integration test for queue_manager.py and facebook.py.

What it covers:
  1. Adds 1 dummy Branch A post (images) and 1 dummy Branch B post (single_image)
     to post_queue.json via queue_manager.add_post()
  2. Publishes Branch A with a scheduled_time 10 minutes in the future
     → should appear as SCHEDULED in Facebook Page Publishing Tools
  3. Publishes Branch B with a scheduled_time in the PAST
     → should publish IMMEDIATELY to the Page
  4. Calls prepare_share_kit() and prints the result
  5. Verifies reschedule logic with a synthetic past-time entry

Usage:
    cd fb-autoposter
    python3 test_facebook.py

Prerequisites:
    FACEBOOK_PAGE_ID and FACEBOOK_ACCESS_TOKEN must be set in .env
    The Page token must have pages_manage_posts permission

WARNING: Steps 2 and 3 make real Facebook API calls and will create
real posts on your Page. Delete them from Facebook after testing.
"""

import logging
import sys
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

try:
    import queue_manager
    from facebook import post_to_page, prepare_share_kit
    from config import FACEBOOK_ACCESS_TOKEN, FACEBOOK_PAGE_ID
except ImportError as exc:
    sys.exit(f"ERROR: Could not import required module — {exc}")

# ── Dummy post data ────────────────────────────────────────────────────────────

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
    # No real image paths — omitted so facebook.py falls back to text-only
    # Replace with real paths to test actual image upload:
    # "before_image": "/path/to/before.jpg",
    # "after_image":  "/path/to/after.jpg",
}

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
    "media_type": "single_image",
    # No real image URL — omitted so facebook.py falls back to text-only
    # Replace to test real image upload:
    # "image_url": "https://images.pexels.com/photos/...jpg",
}


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_add_posts() -> tuple[str, str]:
    """Add 2 dummy entries to post_queue.json. Returns (post_id_a, post_id_b)."""
    log.info("── TEST: add_post() ─────────────────────────────────────────────")

    future_time = (datetime.now() + timedelta(days=2, hours=10)).isoformat(timespec="minutes")
    past_time   = (datetime.now() - timedelta(hours=3)).isoformat(timespec="minutes")

    post_id_a = queue_manager.add_post(
        post_context=DUMMY_BRANCH_A_POST_CONTEXT,
        branch="A",
        scheduled_time=future_time,
    )
    log.info("Added Branch A post: %s  slot=%s", post_id_a, future_time)

    post_id_b = queue_manager.add_post(
        post_context=DUMMY_BRANCH_B_POST_CONTEXT,
        branch="B",
        scheduled_time=past_time,
    )
    log.info("Added Branch B post: %s  slot=%s (in the past)", post_id_b, past_time)

    return post_id_a, post_id_b


def test_schedule_future(post_id_a: str) -> None:
    """
    Publish Branch A post with a scheduled_time 10 minutes in the future.
    Should appear as SCHEDULED in Facebook Page Publishing Tools.
    """
    log.info("── TEST: post_to_page() — scheduled (future) ────────────────────")

    if not FACEBOOK_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        log.warning("FACEBOOK_ACCESS_TOKEN or FACEBOOK_PAGE_ID not set — skipping live API test")
        return

    future_10m = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="minutes")
    log.info("Scheduling Branch A post for %s …", future_10m)

    try:
        fb_id = post_to_page(
            post_context=DUMMY_BRANCH_A_POST_CONTEXT,
            scheduled_time=future_10m,
        )
        queue_manager.update_status(post_id_a, "scheduled", facebook_post_id=fb_id)
        log.info("✅ Scheduled — facebook_post_id=%s", fb_id)
        log.info("   Verify: go to Facebook Page → Publishing Tools → Scheduled Posts")
    except Exception as exc:
        log.error("❌ Scheduling failed: %s", exc)


def test_publish_immediate(post_id_b: str) -> None:
    """
    Publish Branch B post with a scheduled_time in the past.
    Should publish IMMEDIATELY (visible on the Page right away).
    """
    log.info("── TEST: post_to_page() — immediate publish (past time) ──────────")

    if not FACEBOOK_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        log.warning("FACEBOOK_ACCESS_TOKEN or FACEBOOK_PAGE_ID not set — skipping live API test")
        return

    past_time = (datetime.now() - timedelta(hours=2)).isoformat(timespec="minutes")
    log.info("Publishing Branch B post immediately (past scheduled_time=%s) …", past_time)

    try:
        fb_id = post_to_page(
            post_context=DUMMY_BRANCH_B_POST_CONTEXT,
            scheduled_time=past_time,
        )
        queue_manager.update_status(post_id_b, "posted", facebook_post_id=fb_id)
        log.info("✅ Published — facebook_post_id=%s", fb_id)
        log.info("   Verify: check your Facebook Page feed for the new post")
    except Exception as exc:
        log.error("❌ Immediate publish failed: %s", exc)


def test_prepare_share_kit() -> None:
    """Call prepare_share_kit() and print the result."""
    log.info("── TEST: prepare_share_kit() ────────────────────────────────────")

    kit_a = prepare_share_kit(DUMMY_BRANCH_A_POST_CONTEXT)
    kit_b = prepare_share_kit(DUMMY_BRANCH_B_POST_CONTEXT)

    print()
    print("=== Branch A share kit ===")
    print(f"  caption (first 120 chars): {kit_a['caption'][:120].replace(chr(10), ' ')}…")
    print(f"  image_path : {kit_a['image_path']}")
    print(f"  video_path : {kit_a['video_path']}")
    print(f"  image_url  : {kit_a['image_url']}")

    print()
    print("=== Branch B share kit ===")
    print(f"  caption (first 120 chars): {kit_b['caption'][:120].replace(chr(10), ' ')}…")
    print(f"  image_path : {kit_b['image_path']}")
    print(f"  video_path : {kit_b['video_path']}")
    print(f"  image_url  : {kit_b['image_url']}")
    print()


def test_reschedule_logic(post_id_a: str) -> None:
    """Verify check_and_reschedule_approved() on a post with a past scheduled_time."""
    log.info("── TEST: check_and_reschedule_approved() ────────────────────────")

    # Force the post to look like it was approved after its slot passed
    past_time = (datetime.now() - timedelta(days=1)).isoformat(timespec="minutes")
    queue_manager.reschedule_post(
        post_id_a,
        new_time=past_time,
        reason="test: simulating approval after slot passed",
    )

    new_time = queue_manager.check_and_reschedule_approved(post_id_a)
    if new_time:
        log.info("✅ Rescheduled to: %s", new_time)
    else:
        log.info("Post is still in the future — no reschedule needed")


def test_weekly_slots() -> None:
    """Print the 5 slots that assign_weekly_slots() would compute."""
    log.info("── TEST: assign_weekly_slots() ──────────────────────────────────")
    slots = queue_manager.assign_weekly_slots()
    print()
    print("Weekly slot assignments:")
    for i, (branch, sched_time) in enumerate(slots, 1):
        print(f"  [{i}] Branch {branch}  → {sched_time}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 60)
    print("  FACEBOOK + QUEUE MANAGER TEST")
    if not FACEBOOK_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        print("  ⚠️  Facebook credentials not set in .env")
        print("  API call tests will be skipped — queue/logic tests will run")
    print("=" * 60)
    print()

    # 1. Add posts to queue
    post_id_a, post_id_b = test_add_posts()

    # 2. Schedule post (future) — requires Facebook credentials
    test_schedule_future(post_id_a)

    # 3. Publish immediately (past time) — requires Facebook credentials
    test_publish_immediate(post_id_b)

    # 4. Share kit (no API calls)
    test_prepare_share_kit()

    # 5. Reschedule logic
    test_reschedule_logic(post_id_a)

    # 6. Weekly slot computation
    test_weekly_slots()

    print("=" * 60)
    print("  TEST RUN COMPLETE")
    print("  If Facebook tests ran: check Page Publishing Tools for the")
    print("  scheduled post, and the Page feed for the immediate post.")
    print("  Delete both test posts from Facebook when done.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Test interrupted by user")
    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        sys.exit(1)
