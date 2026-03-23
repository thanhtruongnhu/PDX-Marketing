"""
main.py — Weekly orchestration entry point for PDX Remodelling Solutions fb-autoposter.

Runs the full pipeline: generate posts → send to Discord for approval →
publish approved posts to Facebook.

════════════════════════════════════════
CRON SETUP
════════════════════════════════════════
Run once per week on Monday at 7:00am — gives time to approve posts before
the 7:30am Monday slot publishes.

    crontab -e

Add this line (adjust the path to match your installation):

    0 7 * * 1 /usr/bin/python3 /path/to/fb-autoposter/main.py >> /path/to/fb-autoposter/logs/cron.log 2>&1

Verify it is registered:

    crontab -l

Per-run logs are written to logs/run_YYYY-MM-DD.log.
════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Ensure the fb-autoposter directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    ANTHROPIC_API_KEY,
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    FACEBOOK_ACCESS_TOKEN,
    FACEBOOK_PAGE_ID,
    PEXELS_API_KEY,
)

# ── Paths ───────────────────────────────────────────────────────────────────────

_HERE         = Path(__file__).resolve().parent
LOGS_DIR      = _HERE / "logs"
RUN_LOG       = _HERE / "run_log.json"
QUEUE_FILE    = _HERE / "post_queue.json"
INBOX_DIR     = _HERE / "media" / "projects" / "inbox"
PROCESSED_DIR = _HERE / "media" / "projects" / "processed"
TEMP_DIR      = _HERE / "media" / "temp"

log = logging.getLogger("main")

# ── Weekly slot definitions ──────────────────────────────────────────────────────
#
# 4 fixed time slots tuned for peak Facebook engagement (Tue+Sun high-engagement
# days, 7–9am morning window). Slot index 3 (Sunday) is always Branch C.
# day_offset is days from Monday (0=Mon, 1=Tue, 4=Fri, 6=Sun).
#
_WEEK_SLOTS = [
    (0, "07:30"),   # Monday    → slot index 0
    (1, "08:00"),   # Tuesday   → slot index 1
    (4, "08:00"),   # Friday    → slot index 2
    (6, "09:00"),   # Sunday    → slot index 3  (always Branch C)
]

# Which slot indices each branch claims, keyed by branch_a_count.
# Slot index 3 (Sunday) is always Branch C and never included here.
_SLOT_PLAN: dict[int, tuple[list[int], list[int]]] = {
    2: ([0, 2], [1]),        # branch_a_count=2: Mon+Fri → A, Tue → B
    1: ([0],    [1, 2]),     # branch_a_count=1: Mon → A, Tue+Fri → B
    0: ([],     [0, 1, 2]),  # branch_a_count=0: all three → B
}


# ── Logging setup ───────────────────────────────────────────────────────────────

def _setup_logging(run_date: str, dry_run: bool) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    label    = f"{run_date}-dry" if dry_run else run_date
    log_path = LOGS_DIR / f"run_{label}.log"

    fmt      = "%(asctime)s  %(levelname)-7s  %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)


# ── Config validation ────────────────────────────────────────────────────────────

def _validate_config() -> None:
    """Verify all required config keys are present. Exit immediately if any are missing."""
    required = {
        "ANTHROPIC_API_KEY":    ANTHROPIC_API_KEY,
        "DISCORD_BOT_TOKEN":    DISCORD_BOT_TOKEN,
        "DISCORD_CHANNEL_ID":   "" if DISCORD_CHANNEL_ID == 0 else str(DISCORD_CHANNEL_ID),
        "FACEBOOK_PAGE_ID":     FACEBOOK_PAGE_ID,
        "FACEBOOK_ACCESS_TOKEN": FACEBOOK_ACCESS_TOKEN,
        "PEXELS_API_KEY":       PEXELS_API_KEY,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        for name in missing:
            log.error(
                "Missing required config: %s. Add it to .env before running.", name
            )
        sys.exit(1)
    log.info("✅ Config validated")


# ── Slot computation ─────────────────────────────────────────────────────────────

def _compute_week_times(monday: datetime) -> list[str]:
    """Return the 4 ISO-8601 datetime strings for the week starting on monday."""
    times = []
    for day_offset, time_str in _WEEK_SLOTS:
        h, m = map(int, time_str.split(":"))
        slot_dt = (monday + timedelta(days=day_offset)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )
        times.append(slot_dt.isoformat(timespec="minutes"))
    return times


# ── Queue maintenance ────────────────────────────────────────────────────────────

def _prune_resolved_queue(keep_pending: bool = False) -> None:
    """
    Clean the queue before starting a run.

    Normal run (keep_pending=False):
        Wipes the entire queue so stale pending posts from old runs don't
        bleed into the new approval flow or cause the bot to never close.

    --resend-pending (keep_pending=True):
        Only removes fully-resolved posts; preserves pending/max_revisions
        so the bot can re-send them.
    """
    if not QUEUE_FILE.exists():
        return

    queue  = json.loads(QUEUE_FILE.read_text())
    before = len(queue["posts"])

    if keep_pending:
        resolved = {"approved", "discarded", "scheduled", "posted", "failed"}
        queue["posts"] = [p for p in queue["posts"] if p["status"] not in resolved]
    else:
        queue["posts"] = []

    after = len(queue["posts"])
    if before != after:
        QUEUE_FILE.write_text(json.dumps(queue, indent=2))
        log.info(
            "Pruned %d post(s) from queue (%d remaining).",
            before - after, after,
        )


# ── Inbox helpers ────────────────────────────────────────────────────────────────

def _inbox_projects() -> list[Path]:
    """Return sorted list of project folders in inbox/ that haven't been processed."""
    if not INBOX_DIR.exists():
        return []

    try:
        from agents.media_analyzer import MEDIA_LOG
        used: set[str] = set()
        if MEDIA_LOG.exists():
            used = {
                e["project"]
                for e in json.loads(MEDIA_LOG.read_text()).get("used_projects", [])
            }
    except (ImportError, json.JSONDecodeError, OSError):
        used = set()

    return sorted(p for p in INBOX_DIR.iterdir() if p.is_dir() and p.name not in used)


def _process_project(project_path: Path) -> dict | None:
    """
    Analyze a project folder, mark it as used, and move it to processed/.

    Projects are marked immediately after analysis (not after approval) so that
    a second weekly run won't re-use the same project while the first run's
    posts are still awaiting approval.

    Returns media_context dict or None if no usable media was found.
    """
    from agents.media_analyzer import analyze_project, log_used_project

    log.info("Analyzing project: %s", project_path.name)
    media_ctx = analyze_project(project_path)

    if media_ctx is None:
        log.warning("No usable media found in '%s' — skipping.", project_path.name)
        return None

    # Record and move immediately so reruns don't reuse the same project.
    log_used_project(
        project_path.name,
        media_ctx.get("before_image"),
        media_ctx.get("after_image"),
    )
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    dest = PROCESSED_DIR / project_path.name
    shutil.move(str(project_path), str(dest))
    log.info("Moved '%s' → processed/", project_path.name)

    return media_ctx


# ── Branch A generation ──────────────────────────────────────────────────────────

def _build_branch_a_entries(
    projects: list[Path],
    a_slots: list[str],
) -> list[dict]:
    """
    Analyze, generate, and validate Branch A (project story) posts.
    Returns a list of new_post_entry dicts ready for run_approval_flow().
    """
    from agents.generator import generate_project_story_post
    from agents.validator import validate_post
    from discord_bot import new_post_entry

    entries: list[dict] = []

    for project_path, scheduled_time in zip(projects, a_slots):
        media_ctx = _process_project(project_path)
        if media_ctx is None:
            continue

        log.info("Generating Branch A post for: %s", project_path.name)
        post_ctx   = generate_project_story_post(media_ctx)
        validation = validate_post(post_ctx["copy"])

        if not validation["approved"]:
            log.warning(
                "Branch A '%s' did not reach score threshold (score=%.1f) — queuing anyway.",
                project_path.name, validation["score"],
            )
            post_ctx["validator_warning"] = (
                f"Did not reach score threshold (final score: {validation['score']:.1f})"
            )

        entry = new_post_entry(
            branch="A",
            post_context=post_ctx,
            media_context=media_ctx,
            scheduled_time=scheduled_time,
            last_score=validation["score"],
            score_breakdown=validation.get("breakdown", {}),
        )
        entries.append(entry)
        log.info(
            "✅ Branch A ready: %-30s  score=%.1f  slot=%s",
            project_path.name, validation["score"], scheduled_time,
        )

    return entries


# ── Branch B generation ──────────────────────────────────────────────────────────

def _build_branch_b_entries(
    n: int,
    b_slots: list[str],
) -> list[dict]:
    """
    Pick topics, generate, and validate Branch B (maintenance tip) posts.
    Returns a list of new_post_entry dicts ready for run_approval_flow().
    """
    from agents.topic_picker import pick_topics
    from agents.validator import validate_post
    from discord_bot import new_post_entry

    log.info("Generating %d Branch B maintenance post(s)…", n)
    post_contexts = pick_topics(n=n)

    entries: list[dict] = []

    for post_ctx, scheduled_time in zip(post_contexts, b_slots):
        validation = validate_post(post_ctx["copy"])

        if not validation["approved"]:
            log.warning(
                "Branch B '%s' did not reach score threshold (score=%.1f) — queuing anyway.",
                post_ctx.get("topic", "?"), validation["score"],
            )
            post_ctx["validator_warning"] = (
                f"Did not reach score threshold (final score: {validation['score']:.1f})"
            )

        # topic_picker doesn't expose the full maintenance_context; reconstruct
        # a minimal media_context so discord_bot._regenerate_copy() can work.
        media_ctx = {
            "topic":      post_ctx.get("topic", ""),
            "image_path": post_ctx.get("image_path", ""),
        }

        entry = new_post_entry(
            branch="B",
            post_context=post_ctx,
            media_context=media_ctx,
            scheduled_time=scheduled_time,
            last_score=validation["score"],
            score_breakdown=validation.get("breakdown", {}),
        )
        entries.append(entry)
        log.info(
            "✅ Branch B ready: %-40s  score=%.1f  slot=%s",
            post_ctx.get("topic", "?"), validation["score"], scheduled_time,
        )

    return entries


# ── Branch C generation ──────────────────────────────────────────────────────────

def _build_branch_c_entry(scheduled_time: str) -> dict | None:
    """
    Pick a topic, generate, and validate a Branch C saves-optimized post
    (cheat sheet / checklist / top-5 list) for the Sunday slot.

    Reuses topic_picker.pick_topics(n=1) for topic selection and Pexels image
    fetch, then calls generate_saves_post() instead of generate_maintenance_post().

    Returns a new_post_entry dict or None on failure.
    """
    from agents.topic_picker import pick_topics
    from agents.generator import generate_saves_post
    from agents.validator import validate_post
    from discord_bot import new_post_entry

    log.info("── Branch C: generating saves post for Sunday…")
    try:
        # pick_topics returns a maintenance post_context; use only the topic metadata
        # and re-generate with the saves-optimized prompt.
        topic_contexts = pick_topics(n=1)
        if not topic_contexts:
            log.error("pick_topics returned no topics for Branch C.")
            return None
        topic_ctx = topic_contexts[0]

        # Generate saves copy first (image_path may be stale from maintenance copy context).
        post_ctx = generate_saves_post({
            "topic":      topic_ctx.get("topic", ""),
            "post_angle": topic_ctx.get("post_angle", ""),
            "image_path": "",
            "image_url":  "",
        })

        # Re-fetch image scored against the actual saves copy for better alignment.
        from agents.pexels import fetch_image
        img = fetch_image(
            topic_ctx.get("topic", "bathroom renovation"),
            context=post_ctx.get("copy", ""),
        )
        post_ctx["image_path"] = img.get("image_path", "")
        post_ctx["image_url"]  = img.get("image_url", "")
    except Exception as exc:
        log.error("Branch C generation failed: %s", exc)
        return None

    validation = validate_post(post_ctx["copy"])

    if not validation["approved"]:
        log.warning(
            "Branch C '%s' did not reach score threshold (score=%.1f) — queuing anyway.",
            post_ctx.get("topic", "?"), validation["score"],
        )
        post_ctx["validator_warning"] = (
            f"Did not reach score threshold (final score: {validation['score']:.1f})"
        )

    media_ctx = {
        "topic":      post_ctx.get("topic", ""),
        "post_angle": topic_ctx.get("post_angle", ""),
        "image_path": post_ctx.get("image_path", ""),
        "image_url":  post_ctx.get("image_url", ""),
    }

    entry = new_post_entry(
        branch="C",
        post_context=post_ctx,
        media_context=media_ctx,
        scheduled_time=scheduled_time,
        last_score=validation["score"],
        score_breakdown=validation.get("breakdown", {}),
    )
    log.info(
        "✅ Branch C ready: %-40s  score=%.1f  slot=%s",
        post_ctx.get("topic", "?"), validation["score"], scheduled_time,
    )
    return entry


# ── Facebook publishing ──────────────────────────────────────────────────────────

def _publish_approved_posts(post_ids: list[str]) -> None:
    """
    After run_approval_flow() returns, publish all approved posts to the Facebook Page.

    Approved posts are those whose status was set to "approved" by discord_bot
    during the approval flow. Checks whether each post's scheduled_time is still
    in the future (→ schedule it) or has already passed (→ publish immediately).
    """
    from facebook import post_to_page
    import queue_manager as qm

    approved = {p["post_id"]: p for p in qm.get_approved_posts()}

    for post_id in post_ids:
        post = approved.get(post_id)
        if not post:
            continue

        # If the scheduled slot has passed since approval, find the next occurrence.
        new_time = qm.check_and_reschedule_approved(post_id)
        sched_time = new_time or post["scheduled_time"]

        try:
            fb_id = post_to_page(
                post_context=post["post_context"],
                scheduled_time=sched_time,
                media_context=post.get("media_context"),
            )
            try:
                is_future = datetime.fromisoformat(sched_time) > datetime.now()
            except (ValueError, TypeError):
                is_future = False

            new_status = "scheduled" if is_future else "posted"
            qm.update_status(post_id, new_status, facebook_post_id=fb_id)
            log.info(
                "✅ Facebook %s: post_id=%-8s  fb_id=%s  slot=%s",
                new_status, post_id[:8], fb_id, sched_time,
            )
        except Exception as exc:
            log.error("❌ Facebook publish failed for %s: %s", post_id[:8], exc)
            qm.update_status(post_id, "failed")


# ── Discord warning ──────────────────────────────────────────────────────────────

def _notify_discord(message: str) -> None:
    """Send a plain-text notification to the Discord channel via REST."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        import requests
        requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"content": message},
            timeout=10,
        )
    except Exception as exc:
        log.warning("Could not send Discord notification: %s", exc)


# ── First-hour engagement reminder ───────────────────────────────────────────────

def _send_engagement_reminder(post_ids: list[str]) -> None:
    """
    After publishing, notify Discord to engage with each post within the first
    hour of its publish time — early engagement significantly boosts reach.
    """
    if not QUEUE_FILE.exists():
        return
    try:
        queue = json.loads(QUEUE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return

    scheduled = [
        p for p in queue["posts"]
        if p["post_id"] in post_ids and p["status"] in ("scheduled", "posted")
    ]
    if not scheduled:
        return

    lines = [
        "📣 **First-hour engagement reminder**",
        "Like, comment, or reply to each post within 60 min of its publish time — "
        "early engagement is what tells Facebook's algorithm to boost reach.\n",
    ]
    for p in scheduled:
        label = (
            p["post_context"].get("project_name")
            or p["post_context"].get("topic")
            or p["post_id"][:8]
        )
        branch = p.get("branch", "?")
        lines.append(f"• **[Branch {branch}] {label}** → {p['scheduled_time']}")

    _notify_discord("\n".join(lines))


# ── Dry-run printer ──────────────────────────────────────────────────────────────

def _print_dry_run(entries: list[dict]) -> None:
    sep = "═" * 60
    print(f"\n{sep}")
    print("  DRY RUN — posts generated (Discord + Facebook skipped)")
    print(f"{sep}\n")

    for i, entry in enumerate(entries, 1):
        ctx   = entry["post_context"]
        copy  = ctx.get("copy", "")
        tags  = " ".join(ctx.get("hashtags", []))
        topic = ctx.get("project_name") or ctx.get("topic") or "?"
        print(
            f"  [{i}] Branch {entry['branch']}  "
            f"score={entry['last_score']:.1f}  "
            f"slot={entry['scheduled_time']}"
        )
        print(f"       {topic}")
        print(f"       {copy[:120].replace(chr(10), ' ')}…")
        if tags:
            print(f"       {tags}")
        print()

    print(f"{sep}")
    print(f"  ✅ Dry run complete — {len(entries)} post(s) generated")
    print(f"  No Discord or Facebook calls were made.")
    print(f"{sep}\n")


# ── Temp cleanup ─────────────────────────────────────────────────────────────────

def _cleanup_temp(days: int = 7) -> None:
    """Delete files in media/temp/ older than `days` days."""
    if not TEMP_DIR.exists():
        return
    cutoff  = time.time() - days * 86_400
    removed = 0
    for f in TEMP_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        log.info("Cleaned %d temp file(s) older than %d days.", removed, days)


# ── Run log ──────────────────────────────────────────────────────────────────────

def _write_run_log(data: dict) -> None:
    """Append this run's summary to run_log.json."""
    try:
        existing = json.loads(RUN_LOG.read_text()) if RUN_LOG.exists() else []
        if not isinstance(existing, list):
            existing = [existing]
    except (json.JSONDecodeError, OSError):
        existing = []
    existing.append(data)
    RUN_LOG.write_text(json.dumps(existing, indent=2))
    log.info("Run log written → run_log.json")


# ── Main ─────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    run_date = datetime.now().strftime("%Y-%m-%d")
    _setup_logging(run_date, dry_run=args.dry_run)
    log.info("════ fb-autoposter weekly run — %s ════", run_date)

    # ── --reset: wipe logs and temp for a clean slate ─────────────────────────
    if args.reset:
        RUN_LOG.write_text("[]")
        (_HERE / "topic_history.json").write_text('{"history": []}')
        (_HERE / "media_log.json").write_text('{"used_projects": []}')
        removed = 0
        if TEMP_DIR.exists():
            for f in TEMP_DIR.iterdir():
                if f.is_file():
                    f.unlink()
                    removed += 1
        log.info(
            "--reset: cleared run_log.json, topic_history.json, media_log.json, %d temp file(s)",
            removed,
        )

    # ── STEP 1: Startup checks ───────────────────────────────────────────────
    if not args.resend_pending:
        _validate_config()

    # Clean the queue before this run.
    _prune_resolved_queue(keep_pending=args.resend_pending)

    # ── --resend-pending: skip generation, only re-send pending drafts ───────
    if args.resend_pending:
        log.info("--resend-pending: re-sending pending posts to Discord.")
        from discord_bot import run_approval_flow

        pending = [
            p for p in json.loads(QUEUE_FILE.read_text())["posts"]
            if p["status"] in ("pending", "max_revisions")
        ] if QUEUE_FILE.exists() else []

        if not pending:
            log.info("No pending posts found — nothing to re-send.")
            return

        log.info("Found %d pending post(s) from previous run.", len(pending))
        await run_approval_flow([])

        _publish_approved_posts([p["post_id"] for p in pending])
        _cleanup_temp()
        return

    # ── STEP 2: Determine week start ─────────────────────────────────────────
    if args.force_slots:
        anchor = datetime.strptime(args.force_slots, "%Y-%m-%d")
        monday = anchor - timedelta(days=anchor.weekday())
        log.info("Forced week: Monday %s", monday.strftime("%Y-%m-%d"))
    else:
        today  = datetime.now()
        monday = today - timedelta(days=today.weekday())

    week_times = _compute_week_times(monday)  # 4 ISO datetime strings

    # ── STEP 3: Check inbox → determine branch_a_count ───────────────────────
    projects = _inbox_projects()

    if args.branch_b_only:
        branch_a_count = 0
        log.info("--branch-b-only: skipping Branch A.")
    elif len(projects) == 0:
        branch_a_count = 0
        log.info("Inbox empty — skipping project story posts this week.")
    elif len(projects) == 1:
        branch_a_count = 1
        log.info("Inbox low — processing 1 project story post this week.")
    else:
        branch_a_count = 2
        log.info("Inbox has %d project(s) — processing 2 project story posts.", len(projects))

    log.info(
        "✅ Inbox checked (%d project(s) found)  branch_a_count=%d",
        len(projects), branch_a_count,
    )

    # ── STEP 4: Assign schedule slots ────────────────────────────────────────
    a_indices, b_indices = _SLOT_PLAN[branch_a_count]
    a_slots = [week_times[i] for i in a_indices]
    b_slots = [week_times[i] for i in b_indices]
    c_slot  = week_times[3]   # Sunday is always Branch C

    # Inbox warning level for run log + Discord notification
    inbox_warning: str | None = None
    if len(projects) == 0:
        inbox_warning = "empty"
    elif len(projects) == 1:
        inbox_warning = "low"

    # ── STEP 5: Generate content ──────────────────────────────────────────────
    all_entries:   list[dict] = []
    projects_used: list[str]  = []
    topics_used:   list[str]  = []

    if not args.branch_b_only and branch_a_count > 0:
        log.info("── Branch A: generating %d post(s)…", branch_a_count)
        a_entries = _build_branch_a_entries(projects[:branch_a_count], a_slots)
        all_entries.extend(a_entries)
        projects_used = [
            e["media_context"].get("project_name", "") for e in a_entries
        ]
        log.info("✅ Branch A: %d post(s) generated", len(a_entries))

    if not args.branch_a_only:
        maintenance_count = len(b_slots)
        if maintenance_count > 0:
            log.info("── Branch B: generating %d post(s)…", maintenance_count)
            b_entries = _build_branch_b_entries(maintenance_count, b_slots)
            all_entries.extend(b_entries)
            topics_used = [
                e["post_context"].get("topic", "") for e in b_entries
            ]
            log.info("✅ Branch B: %d post(s) generated", len(b_entries))

        log.info("── Branch C: generating saves post for Sunday…")
        c_entry = _build_branch_c_entry(c_slot)
        if c_entry:
            all_entries.append(c_entry)
            topics_used.append(c_entry["post_context"].get("topic", ""))
            log.info("✅ Branch C: 1 post generated")

    log.info("Total posts this run: %d", len(all_entries))

    # Collect post_ids for run log and post-approval publishing.
    run_post_ids = [e["post_id"] for e in all_entries]

    # ── STEP 6: Dry run OR Discord + Facebook ─────────────────────────────────
    if args.dry_run:
        _print_dry_run(all_entries)
    else:
        # Send inbox warning to Discord before drafts arrive.
        if inbox_warning == "empty":
            _notify_discord(
                "⚠️ **Inbox EMPTY** — no project media available. "
                "All posts this week are maintenance tips. "
                "Upload new before/after folders to `media/projects/inbox/` soon."
            )
        elif inbox_warning == "low":
            _notify_discord(
                "⚠️ **Inbox Low** — only 1 project folder remaining after this week. "
                "Consider uploading new content to `media/projects/inbox/` soon."
            )

        from discord_bot import run_approval_flow

        log.info("Sending %d draft(s) to Discord for approval…", len(all_entries))
        await run_approval_flow(all_entries)

        # Publish all posts that were approved during the Discord flow.
        log.info("Approval flow complete. Publishing approved posts to Facebook…")
        _publish_approved_posts(run_post_ids)

        # Remind the operator to engage within the first hour of each publish time.
        _send_engagement_reminder(run_post_ids)

    # ── STEP 7: Cleanup + run log ─────────────────────────────────────────────
    _cleanup_temp()
    _write_run_log({
        "run_date":       run_date,
        "branch_a_posts": len([e for e in all_entries if e["branch"] == "A"]),
        "branch_b_posts": len([e for e in all_entries if e["branch"] == "B"]),
        "branch_c_posts": len([e for e in all_entries if e["branch"] == "C"]),
        "inbox_warning":  inbox_warning,
        "post_ids":       run_post_ids,
        "topics_used":    topics_used,
        "projects_used":  projects_used,
    })
    log.info("════ Run complete ════")


# ── CLI ───────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PDX Remodelling Solutions fb-autoposter — weekly run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py                     # full weekly run\n"
            "  python main.py --dry-run           # generate posts, print to terminal\n"
            "  python main.py --branch-a-only     # project story posts only\n"
            "  python main.py --branch-b-only     # maintenance tip posts only\n"
            "  python main.py --resend-pending    # re-send pending posts to Discord\n"
            "  python main.py --force-slots 2026-03-17  # use week of given date\n"
            "  python main.py --reset                  # clear logs/temp, then run\n"
            "  python main.py --reset --dry-run        # clear logs/temp, then dry run\n"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate posts but skip Discord and Facebook — prints to terminal only",
    )
    p.add_argument(
        "--branch-a-only",
        action="store_true",
        help="Run Branch A (project story) only, skip Branch B",
    )
    p.add_argument(
        "--branch-b-only",
        action="store_true",
        help="Run Branch B (maintenance tip) only, skip Branch A",
    )
    p.add_argument(
        "--resend-pending",
        action="store_true",
        help="Skip content generation — re-send pending posts from post_queue.json to Discord",
    )
    p.add_argument(
        "--force-slots",
        metavar="YYYY-MM-DD",
        help="Override slot calculation to use the week containing this date (for testing / catch-up)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Clear run_log.json, topic_history.json, and media/temp/* before running (useful for fresh test runs)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(args))
