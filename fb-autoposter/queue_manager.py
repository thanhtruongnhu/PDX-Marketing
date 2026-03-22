"""
queue_manager.py — Post queue management for PDX Remodelling Solutions fb-autoposter.

Handles scheduling slots, hold logic, reschedule-after-approval, and
inbox stock warnings. All state is persisted to post_queue.json.

Public API:
    add_post(post_context, branch, scheduled_time, media_context=None) → post_id
    update_status(post_id, status)
    get_pending_posts()  → list
    get_approved_posts() → list
    reschedule_post(post_id, new_time, reason=...)
    get_next_slot(day_of_week, time) → ISO datetime string
    assign_weekly_slots() → [(branch, scheduled_time), ...]
    check_and_reschedule_approved(post_id) → new_time or None
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_HERE      = Path(__file__).resolve().parent
QUEUE_FILE = _HERE / "post_queue.json"
INBOX_DIR  = _HERE / "media" / "projects" / "inbox"

VALID_STATUSES = {"pending", "approved", "scheduled", "posted", "discarded", "failed"}

# Maps weekday name → Python weekday int (Monday = 0)
_DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ── Queue I/O ──────────────────────────────────────────────────────────────────

def _load_queue() -> dict:
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text())
    return {"posts": []}


def _save_queue(queue: dict) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


# ── Inbox status ───────────────────────────────────────────────────────────────

def _inbox_status() -> Optional[str]:
    """Return 'empty', 'low', or None based on unused project folder count."""
    if not INBOX_DIR.exists():
        return "empty"
    folders = [p for p in INBOX_DIR.iterdir() if p.is_dir()]
    if len(folders) == 0:
        return "empty"
    if len(folders) == 1:
        return "low"
    return None


# ── Schedule helpers ───────────────────────────────────────────────────────────

def get_next_slot(day_of_week: str, time: str) -> str:
    """
    Return the next future occurrence of a given weekday + time as an ISO string.

    Args:
        day_of_week: e.g. "Monday"
        time:        e.g. "10:00"

    Returns:
        ISO-8601 datetime string (minute precision) for the next occurrence.
        If today is the target day and the slot hasn't passed yet, returns today.
        Otherwise returns the next calendar occurrence.
    """
    target_weekday = _DAY_MAP[day_of_week.lower()]
    hour, minute = map(int, time.split(":"))
    now = datetime.now()

    days_ahead = target_weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    elif days_ahead == 0:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= candidate:
            days_ahead = 7

    slot_dt = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return slot_dt.isoformat(timespec="minutes")


# ── Public API ─────────────────────────────────────────────────────────────────

def add_post(
    post_context: dict,
    branch: str,
    scheduled_time: str,
    media_context: Optional[dict] = None,
) -> str:
    """
    Write a new post entry to post_queue.json with status 'pending'.

    Args:
        post_context:    generated post dict (copy, hashtags, media_type, …)
        branch:          "A" (project story) or "B" (maintenance tip)
        scheduled_time:  ISO-8601 datetime string for intended publish time
        media_context:   optional media_context dict from media_analyzer

    Returns:
        post_id (uuid string)
    """
    queue    = _load_queue()
    post_id  = str(uuid.uuid4())
    inbox_w  = _inbox_status()

    entry = {
        "post_id":            post_id,
        "branch":             branch,
        "status":             "pending",
        "revision_count":     0,
        "scheduled_time":     scheduled_time,
        "rescheduled_from":   None,
        "rescheduled_reason": None,
        "post_context":       post_context,
        "media_context":      media_context or {},
        "feedback_history":   [],
        "facebook_post_id":   None,
        "inbox_warning":      inbox_w,
    }
    queue["posts"].append(entry)
    _save_queue(queue)
    log.info("Queued post %s  branch=%s  slot=%s", post_id, branch, scheduled_time)
    return post_id


def update_status(post_id: str, status: str, facebook_post_id: Optional[str] = None) -> None:
    """
    Update the status field (and optionally facebook_post_id) for a given post.

    Raises:
        ValueError if status is not in VALID_STATUSES
        KeyError   if post_id is not found
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    queue = _load_queue()
    for post in queue["posts"]:
        if post["post_id"] == post_id:
            post["status"] = status
            if facebook_post_id is not None:
                post["facebook_post_id"] = facebook_post_id
            _save_queue(queue)
            log.info("Post %s → status='%s'", post_id, status)
            return

    raise KeyError(f"Post {post_id} not found in queue")


def get_pending_posts() -> list:
    """
    Return all posts with status 'pending'.
    Used by discord_bot.py on restart to re-send drafts that lost their
    Discord message (e.g. after a bot crash).
    """
    return [p for p in _load_queue()["posts"] if p["status"] == "pending"]


def get_approved_posts() -> list:
    """
    Return all posts with status 'approved'.
    Used by facebook.py to know what to publish next.
    """
    return [p for p in _load_queue()["posts"] if p["status"] == "approved"]


def reschedule_post(
    post_id: str,
    new_time: str,
    reason: str = "approved after scheduled time",
) -> None:
    """
    Update scheduled_time for a post and record the old time + reason.

    Raises:
        KeyError if post_id is not found
    """
    queue = _load_queue()
    for post in queue["posts"]:
        if post["post_id"] == post_id:
            old_time = post.get("scheduled_time")
            post["rescheduled_from"]   = old_time
            post["rescheduled_reason"] = reason
            post["scheduled_time"]     = new_time
            _save_queue(queue)
            log.info(
                "Post %s rescheduled: %s → %s  reason='%s'",
                post_id, old_time, new_time, reason,
            )
            return

    raise KeyError(f"Post {post_id} not found in queue")


def check_and_reschedule_approved(post_id: str) -> Optional[str]:
    """
    Called after a post is approved in Discord.

    If the post's scheduled_time is still in the future: do nothing, return None.
    If it has already passed: reschedule to the next occurrence of the same
    weekday + time and return the new ISO datetime string.

    This mirrors the spec logic:
      "approved a Monday post on Tuesday →
       reschedule to next Monday at 10:00am"
    """
    queue = _load_queue()
    for post in queue["posts"]:
        if post["post_id"] != post_id:
            continue

        sched_str = post.get("scheduled_time", "")
        try:
            sched_dt = datetime.fromisoformat(sched_str)
        except (ValueError, TypeError):
            log.warning("Post %s has unparseable scheduled_time '%s'", post_id, sched_str)
            return None

        if datetime.now() <= sched_dt:
            return None  # still in the future — nothing to do

        # Rebuild the next slot for the same weekday and time
        weekday_name = sched_dt.strftime("%A")   # e.g. "Monday"
        time_str     = sched_dt.strftime("%H:%M")  # e.g. "10:00"
        new_time     = get_next_slot(weekday_name, time_str)
        reschedule_post(post_id, new_time, reason="approved after scheduled time")
        return new_time

    raise KeyError(f"Post {post_id} not found in queue")


def assign_weekly_slots() -> list:
    """
    Compute the 5 scheduled time slots for the upcoming weekly run using
    WEEKLY_SCHEDULE from config.py.

    Also writes inbox_warning to the queue metadata (top-level key) so
    discord_bot.py can prepend the warning to the first draft that week.

    Returns:
        List of (branch, scheduled_time_iso) tuples — one per slot.
    """
    from config import WEEKLY_SCHEDULE  # imported here to avoid circular imports

    queue      = _load_queue()
    inbox_warn = _inbox_status()
    queue["inbox_warning"] = inbox_warn
    _save_queue(queue)

    if inbox_warn == "empty":
        log.warning(
            "🚨 Inbox EMPTY — no project media available. "
            "Running maintenance tip posts only this week."
        )
    elif inbox_warn == "low":
        log.warning(
            "⚠️  Inbox LOW — only 1 project folder remaining. "
            "Upload new before/after content soon."
        )

    slots = []
    for slot in WEEKLY_SCHEDULE:
        scheduled_time = get_next_slot(slot["day"], slot["time"])
        slots.append((slot["branch"], scheduled_time))
        log.info(
            "Slot assigned: branch=%s  day=%s  time=%s  → %s",
            slot["branch"], slot["day"], slot["time"], scheduled_time,
        )

    return slots
