"""
discord_bot.py — Discord approval workflow for PDX Remodelling Solutions autoposter.

Sends formatted post drafts to a private Discord channel and waits for your reply.
Handles the full revision loop (max 3 rounds) and returns approved post contexts.

Usage:
    import asyncio
    from discord_bot import run_approval_flow, new_post_entry

    approved = asyncio.run(run_approval_flow(post_entries))

Setup (one-time):
    1. Create a bot at discord.com/developers/applications
    2. Under Bot → enable "Message Content Intent" (privileged)
    3. Copy the bot token → DISCORD_BOT_TOKEN in .env
    4. Invite the bot to your server with scopes: bot + permissions: Send Messages,
       Read Message History, Attach Files
    5. Add DISCORD_CHANNEL_ID and DISCORD_OWNER_ID to .env

post_entry schema (use new_post_entry() to build):
    {
        "post_id":            str,      # uuid
        "branch":             "A"|"B",  # A = project story, B = maintenance tip
        "status":             str,      # pending | approved | discarded | max_revisions
        "revision_count":     int,      # revisions sent so far (max 3)
        "scheduled_time":     str,      # ISO-8601 datetime
        "last_score":         float,    # validator score 0–10
        "score_breakdown":    dict,     # per-dimension scores from validator
        "post_context":       dict,     # {copy, hashtags, ...}
        "media_context":      dict,     # original media data (project_dir for remedia)
        "feedback_history":   list,     # [{round, notes, type}]
        "discord_message_id": int|None, # most-recent draft message sent to Discord
    }
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord

sys.path.insert(0, os.path.dirname(__file__))

from config import (
    DISCORD_APPROVAL_TIMEOUT,
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    DISCORD_OWNER_ID,
)

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
QUEUE_FILE = _HERE / "post_queue.json"

SEP = "━" * 32

# Maps validator dimension keys → human-readable labels for the draft message
_DIMENSION_LABELS = {
    "hook_strength":  "Hook",
    "clarity":        "Clarity",
    "cta":            "CTA",
    "tone_match":     "Tone",
    "length":         "Length",
    "value_vs_pitch": "Value",
}


# ── Queue helpers ──────────────────────────────────────────────────────────────

def _load_queue() -> dict:
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text())
    return {"posts": []}


def _save_queue(queue: dict) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def _find_post(queue: dict, post_id: str) -> Optional[dict]:
    for p in queue["posts"]:
        if p["post_id"] == post_id:
            return p
    return None


def _update_post(queue: dict, post_id: str, updates: dict) -> None:
    for p in queue["posts"]:
        if p["post_id"] == post_id:
            p.update(updates)
            return


def _upsert_post(queue: dict, entry: dict) -> None:
    """Insert or update a post entry by post_id."""
    for i, p in enumerate(queue["posts"]):
        if p["post_id"] == entry["post_id"]:
            queue["posts"][i].update(entry)
            return
    queue["posts"].append(entry)


# ── Message formatting ─────────────────────────────────────────────────────────

def _fmt_scheduled(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%a %b %-d @ %-I:%M%p")   # Mon Mar 16 @ 10:00AM
    except (ValueError, TypeError):
        return str(iso_str)


def _fmt_breakdown(breakdown: dict) -> str:
    """Return  ✅ Hook  ✅ Tone  ⚠️ CTA  ...  based on per-dimension scores."""
    parts = []
    for key, label in _DIMENSION_LABELS.items():
        if key in breakdown:
            icon = "✅" if breakdown[key] >= 7.5 else "⚠️"
            parts.append(f"{icon} {label}")
    return "  ".join(parts)


def _build_draft_text(entry: dict, revision_label: str = "", position_label: str = "") -> str:
    branch    = entry["branch"]
    post_ctx  = entry["post_context"]
    score     = entry.get("last_score", 0.0)
    breakdown = entry.get("score_breakdown", {})
    sched_str = _fmt_scheduled(entry.get("scheduled_time", "TBD"))
    copy      = post_ctx.get("copy", "")
    hashtags  = " ".join(post_ctx.get("hashtags", []))
    bd_str    = _fmt_breakdown(breakdown)
    rev_sfx   = f" — {revision_label}" if revision_label else ""
    pos_sfx   = f"  ·  {position_label}" if position_label else ""

    if branch == "A":
        header = (
            f"{SEP}\n"
            f"📸 Project Story Draft{rev_sfx}{pos_sfx}\n"
            f"Scheduled: {sched_str}\n"
            f"Project: {post_ctx.get('project_name', 'Unknown')}\n"
            f"Media: {post_ctx.get('media_type', 'images')}\n"
            f"{SEP}"
        )
        commands = (
            "Reply with:\n"
            "✅ approve\n"
            "❌ reject [your notes]\n"
            "🖼️ remedia [your notes]"
        )
    elif branch == "C":
        header = (
            f"{SEP}\n"
            f"💾 Saves Post Draft{rev_sfx}{pos_sfx}\n"
            f"Scheduled: {sched_str}\n"
            f"Topic: {post_ctx.get('topic', 'Unknown')}\n"
            f"{SEP}"
        )
        commands = (
            "Reply with:\n"
            "✅ approve\n"
            "❌ reject [your notes]\n"
            "🖼️ remedia [your notes]"
        )
    else:
        header = (
            f"{SEP}\n"
            f"🏠 Maintenance Tip Draft{rev_sfx}{pos_sfx}\n"
            f"Scheduled: {sched_str}\n"
            f"Topic: {post_ctx.get('topic', 'Unknown')}\n"
            f"{SEP}"
        )
        commands = (
            "Reply with:\n"
            "✅ approve\n"
            "❌ reject [your notes]\n"
            "🖼️ remedia [your notes]"
        )

    score_line = f"Validator Score: {score:.1f}/10"
    if bd_str:
        score_line += f"\n{bd_str}"

    footer = f"{SEP}\n{score_line}\n\n{commands}\n{SEP}"
    body   = f"{copy}\n\n{hashtags}" if hashtags else copy
    return f"{header}\n\n{body}\n\n{footer}"


def _build_share_kit_text(entry: dict) -> str:
    post_ctx  = entry["post_context"]
    copy      = post_ctx.get("copy", "")
    hashtags  = " ".join(post_ctx.get("hashtags", []))

    try:
        dt       = datetime.fromisoformat(entry.get("scheduled_time", ""))
        sched_ln = dt.strftime("%A %B %-d @ %-I:%M%p").lower()
    except (ValueError, TypeError):
        sched_ln = entry.get("scheduled_time", "TBD")

    if entry["branch"] == "A" and post_ctx.get("media_type") == "video":
        media_note = "🎬 Video attached above"
    else:
        media_note = "📎 Image attached above"

    body = f"{copy}\n\n{hashtags}" if hashtags else copy
    return (
        f"{SEP}\n"
        f"✅ Scheduled to your Facebook Page\n"
        f"{sched_ln}\n\n"
        f"📦 Community Group Share Kit\n"
        f"Copy this caption and share in your groups whenever ready:\n\n"
        f"{body}\n\n"
        f"{media_note}\n"
        f"{SEP}"
    )


# ── Discord Bot ────────────────────────────────────────────────────────────────

class DiscordApprovalBot(discord.Client):
    """
    Sends post drafts to a Discord channel and handles the approval loop.

    Each post entry in _initial_entries is persisted to post_queue.json.
    On restart, pending posts are automatically re-sent so nothing gets lost.
    Replies are routed to the correct post via Discord's native Reply feature
    (message.reference.message_id).
    """

    def __init__(self, initial_entries: list):
        intents = discord.Intents.default()
        intents.message_content = True          # requires privileged intent in Dev Portal
        super().__init__(intents=intents)

        self._initial_entries = initial_entries
        self._channel: Optional[discord.TextChannel] = None
        self._msg_to_post: dict[int, str] = {}  # discord message id → post_id
        self._done_event = asyncio.Event()
        self.approved_posts: list[dict] = []    # collected on approve

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_ready(self):
        log.info("Discord bot online as %s (id=%d)", self.user, self.user.id)

        self._channel = self.get_channel(DISCORD_CHANNEL_ID)
        if self._channel is None:
            log.error("Channel %d not found — verify DISCORD_CHANNEL_ID", DISCORD_CHANNEL_ID)
            self._done_event.set()
            await self.close()
            return

        # Persist new entries to queue
        queue = _load_queue()
        for entry in self._initial_entries:
            _upsert_post(queue, entry)
        _save_queue(queue)

        # Re-send drafts for all unresolved posts (handles bot restarts)
        pending = [
            p for p in queue["posts"]
            if p["status"] in ("pending", "max_revisions")
        ]
        if not pending:
            log.info("No pending posts — nothing to do")
            self._done_event.set()
            await self.close()
            return

        await self._send_next_pending()

    async def on_message(self, message: discord.Message):
        if message.channel.id != DISCORD_CHANNEL_ID:
            return
        if message.author == self.user:
            return
        if DISCORD_OWNER_ID and message.author.id != DISCORD_OWNER_ID:
            return

        # Route to the correct post via Discord's Reply feature
        post_id = None
        if message.reference and message.reference.message_id:
            post_id = self._msg_to_post.get(message.reference.message_id)

        if post_id is None:
            # Fall back: target the oldest pending post (lowest discord_message_id).
            # This lets users approve posts sequentially by typing commands directly
            # without needing Discord's reply-thread feature every time.
            queue = _load_queue()
            active = [p for p in queue["posts"] if p["status"] in ("pending", "max_revisions")]
            if not active:
                return
            with_msg = [p for p in active if p.get("discord_message_id")]
            if with_msg:
                post_id = min(with_msg, key=lambda p: p["discord_message_id"])["post_id"]
            elif len(active) == 1:
                post_id = active[0]["post_id"]
            else:
                await message.channel.send(
                    "Sorry, I couldn't tell which post this reply is for.\n"
                    "Please **reply directly to the draft message** (right-click → Reply), then type:\n"
                    "`approve` | `reject [notes]` | `remedia [notes]`"
                )
                return

        queue = _load_queue()
        entry = _find_post(queue, post_id)
        if entry is None:
            log.warning("post_id %s not found in queue", post_id)
            return

        text  = message.content.strip()
        lower = text.lower()

        # ── Max-revisions state: only approve / discard / override ──
        if entry["status"] == "max_revisions":
            if lower.startswith("approve"):
                await self._handle_approve(entry, queue)
            elif lower.startswith("discard"):
                await self._handle_discard(entry, queue)
            elif lower.startswith("override "):
                await self._handle_override(entry, queue, text[9:].strip())
            else:
                await message.channel.send(
                    "⚠️ This post is at the maximum revision limit.\n"
                    "Reply with: `approve` / `discard` / `override [new copy]`"
                )
            return

        # ── Normal pending state ──
        if lower.startswith("approve"):
            await self._handle_approve(entry, queue)
        elif lower.startswith("reject"):
            await self._handle_reject(entry, queue, text[6:].strip())
        elif lower.startswith("remedia"):
            await self._handle_remedia(entry, queue, text[7:].strip())
        else:
            tip = " | `remedia [notes]`"
            await message.channel.send(
                "Sorry, I didn't understand that. Please reply with:\n"
                f"`approve` | `reject [notes]`{tip}"
            )

    # ── Sending helpers ────────────────────────────────────────────────────────

    async def _send_draft(self, entry: dict, revision_label: str = "", position_label: str = "") -> discord.Message:
        """Format and send a draft message with any media attachments. Retries on connection errors."""
        text  = _build_draft_text(entry, revision_label, position_label)
        files = self._collect_attachments(entry)

        for attempt in range(1, 4):
            try:
                # Discord limit: 2000 chars per message. If over, split header+body / footer.
                if len(text) > 1900:
                    split = text.rfind(SEP)
                    if split > 0:
                        first = await self._channel.send(text[:split].rstrip(), files=files)
                        msg   = await self._channel.send(text[split:])
                        self._msg_to_post[first.id] = entry["post_id"]
                    else:
                        first = await self._channel.send(text[:1900], files=files)
                        msg   = await self._channel.send(text[1900:2000])
                        self._msg_to_post[first.id] = entry["post_id"]
                else:
                    msg = await self._channel.send(text, files=files)

                self._msg_to_post[msg.id] = entry["post_id"]
                queue = _load_queue()
                _update_post(queue, entry["post_id"], {"discord_message_id": msg.id})
                _save_queue(queue)
                log.info("Sent draft for %s  msg_id=%d", entry["post_id"][:8], msg.id)
                return msg

            except Exception as exc:
                if attempt < 3:
                    wait = 2 ** attempt  # 2s, 4s
                    log.warning(
                        "Send failed for %s (attempt %d/3): %s — retrying in %ds",
                        entry["post_id"][:8], attempt, exc, wait,
                    )
                    await asyncio.sleep(wait)
                    files = self._collect_attachments(entry)  # re-open file handles
                else:
                    log.error("Failed to send draft for %s after 3 attempts: %s", entry["post_id"][:8], exc)
                    raise

    async def _resend_max_revisions(self, entry: dict, position_label: str = ""):
        """Re-send the max-revisions prompt on bot restart."""
        pos_line = f"  ·  {position_label}" if position_label else ""
        msg = await self._channel.send(
            f"⚠️ **Revision limit reached**{pos_line}\n"
            f"> {entry['post_context'].get('topic') or entry['post_context'].get('project_name', entry['post_id'])}\n\n"
            f"Reply with: `approve` / `discard` / `override [new copy]`"
        )
        self._msg_to_post[msg.id] = entry["post_id"]

    def _collect_attachments(self, entry: dict) -> list:
        files     = []
        media_ctx = entry.get("media_context") or {}
        post_ctx  = entry.get("post_context") or {}

        if entry["branch"] == "A":
            if media_ctx.get("type") == "video":
                vpath = media_ctx.get("video_path")
                if vpath and Path(vpath).exists():
                    files.append(discord.File(vpath))
            else:
                for key in ("before_image", "after_image"):
                    p = media_ctx.get(key)
                    if p and Path(p).exists():
                        files.append(discord.File(p))
        else:
            img = post_ctx.get("image_path") or media_ctx.get("image_path")
            if img and Path(img).exists():
                files.append(discord.File(img))

        return files

    # ── Action handlers ────────────────────────────────────────────────────────

    async def _handle_approve(self, entry: dict, queue: dict):
        _update_post(queue, entry["post_id"], {"status": "approved"})
        _save_queue(queue)
        self.approved_posts.append(entry["post_context"])
        sched = _fmt_scheduled(entry.get("scheduled_time", ""))
        await self._channel.send(f"✅ Scheduled to your Facebook Page — {sched}")
        log.info("Post %s approved", entry["post_id"][:8])
        await self._check_done()

    async def _handle_reject(self, entry: dict, queue: dict, notes: str):
        rev = entry.get("revision_count", 0)
        if rev >= 3:
            await self._send_max_revisions_gate(entry, queue)
            return

        new_rev = rev + 1
        history = list(entry.get("feedback_history", []))
        history.append({"round": new_rev, "notes": notes, "type": "reject"})
        _update_post(queue, entry["post_id"], {
            "revision_count": new_rev,
            "feedback_history": history,
        })
        _save_queue(queue)

        await self._channel.send(
            f"🔄 Revision {new_rev} in progress — based on your note: *\"{notes}\"*\n"
            f"Regenerating copy…"
        )
        try:
            updated = await asyncio.to_thread(self._regenerate_copy, entry, notes)
        except Exception as exc:
            log.error("Regeneration failed: %s", exc)
            await self._channel.send(f"❌ Regeneration failed: {exc}")
            return

        _update_post(queue, entry["post_id"], {
            "post_context":   updated["post_context"],
            "last_score":     updated["last_score"],
            "score_breakdown": updated.get("score_breakdown", {}),
        })
        _save_queue(queue)

        queue = _load_queue()
        fresh = _find_post(queue, entry["post_id"])
        await self._send_draft(fresh, revision_label=f"Revision {new_rev}")

    async def _handle_remedia(self, entry: dict, queue: dict, notes: str):
        rev = entry.get("revision_count", 0)
        if rev >= 3:
            await self._send_max_revisions_gate(entry, queue)
            return

        new_rev = rev + 1
        rtype   = "remedia" if entry["branch"] == "A" else "reimage"
        history = list(entry.get("feedback_history", []))
        history.append({"round": new_rev, "notes": notes, "type": rtype})
        _update_post(queue, entry["post_id"], {
            "revision_count": new_rev,
            "feedback_history": history,
        })
        _save_queue(queue)

        label = "new media" if entry["branch"] == "A" else "new image"
        await self._channel.send(f"🔄 Revision {new_rev} ({label}) in progress…")
        try:
            if entry["branch"] == "A":
                updated = await asyncio.to_thread(self._rerun_with_new_media, entry, notes)
            else:
                updated = await asyncio.to_thread(self._reimage_branch_bc, entry, notes)
        except Exception as exc:
            log.error("Remedia failed: %s", exc)
            await self._channel.send(f"❌ Remedia failed: {exc}")
            return

        _update_post(queue, entry["post_id"], {
            "post_context":    updated["post_context"],
            "media_context":   updated.get("media_context", entry.get("media_context")),
            "last_score":      updated["last_score"],
            "score_breakdown": updated.get("score_breakdown", {}),
        })
        _save_queue(queue)

        queue = _load_queue()
        fresh = _find_post(queue, entry["post_id"])
        await self._send_draft(fresh, revision_label=f"Revision {new_rev} ({label})")

    async def _handle_override(self, entry: dict, queue: dict, custom_copy: str):
        entry["post_context"]["copy"] = custom_copy
        _update_post(queue, entry["post_id"], {
            "status":       "approved",
            "post_context": entry["post_context"],
        })
        _save_queue(queue)
        self.approved_posts.append(entry["post_context"])
        sched = _fmt_scheduled(entry.get("scheduled_time", ""))
        await self._channel.send(f"✅ Approved with custom copy — scheduled for Facebook: {sched}")
        log.info("Post %s approved via override", entry["post_id"][:8])
        await self._check_done()

    async def _handle_discard(self, entry: dict, queue: dict):
        label = (
            entry["post_context"].get("topic")
            or entry["post_context"].get("project_name")
            or entry["post_id"][:8]
        )
        _update_post(queue, entry["post_id"], {"status": "discarded"})
        _save_queue(queue)
        await self._channel.send(f"🗑️ Post discarded: *{label}*")
        log.info("Post %s discarded", entry["post_id"][:8])
        await self._check_done()

    async def _send_max_revisions_gate(self, entry: dict, queue: dict):
        """Transition a post to max_revisions state and send the override prompt."""
        _update_post(queue, entry["post_id"], {"status": "max_revisions"})
        _save_queue(queue)
        msg = await self._channel.send(
            f"⚠️ This post has reached the maximum revision limit.\n"
            f"Please review it manually and let me know how to proceed.\n\n"
            f"Reply with: `approve` / `discard` / `override [new copy]`"
        )
        self._msg_to_post[msg.id] = entry["post_id"]

    # ── Sync regeneration (called via asyncio.to_thread) ──────────────────────

    def _regenerate_copy(self, entry: dict, feedback_notes: str) -> dict:
        """Regenerate post copy with reviewer feedback. Returns updated fields."""
        from agents.generator import generate_maintenance_post, generate_project_story_post, generate_saves_post
        from agents.validator import validate_post

        media_ctx = dict(entry.get("media_context") or {})

        if entry["branch"] == "A":
            ctx = dict(media_ctx)
            ctx["content_context"] = (
                ctx.get("content_context", "")
                + f"\n\nRevision feedback: {feedback_notes}"
            )
            result = generate_project_story_post(ctx, verbose=False)
        elif entry["branch"] == "C":
            ctx = dict(media_ctx)
            ctx["post_angle"] = (
                ctx.get("post_angle", "")
                + f". Revision feedback: {feedback_notes}"
            )
            result = generate_saves_post(ctx, verbose=False)
        else:
            ctx = dict(media_ctx)
            ctx["post_angle"] = (
                ctx.get("post_angle", "")
                + f". Revision feedback: {feedback_notes}"
            )
            result = generate_maintenance_post(ctx, verbose=False)

        validation = validate_post(result["copy"])
        return {
            "post_context":    result,
            "last_score":      validation["score"],
            "score_breakdown": validation.get("breakdown", {}),
        }

    def _rerun_with_new_media(self, entry: dict, feedback_notes: str) -> dict:
        """Re-analyze the project and regenerate copy. Returns updated fields."""
        from agents.generator import generate_project_story_post
        from agents.media_analyzer import analyze_project
        from agents.validator import validate_post

        project_dir = (entry.get("media_context") or {}).get("project_dir")
        if not project_dir:
            raise ValueError(
                "media_context.project_dir is missing — cannot run remedia.\n"
                "Ensure the post entry was created from analyze_next_project()."
            )

        new_media_ctx = analyze_project(project_dir)
        if new_media_ctx is None:
            raise ValueError(f"Could not re-analyze project at {project_dir}")

        new_media_ctx["content_context"] = (
            new_media_ctx.get("content_context", "")
            + f"\n\nRemedia feedback: {feedback_notes}"
        )
        result     = generate_project_story_post(new_media_ctx, verbose=False)
        validation = validate_post(result["copy"])
        return {
            "post_context":    result,
            "media_context":   new_media_ctx,
            "last_score":      validation["score"],
            "score_breakdown": validation.get("breakdown", {}),
        }

    def _reimage_branch_bc(self, entry: dict, notes: str) -> dict:
        """Swap Pexels image for Branch B or C. Copy text stays unchanged."""
        from agents.pexels import fetch_image

        media_ctx = dict(entry.get("media_context") or {})
        topic     = media_ctx.get("topic") or entry["post_context"].get("topic", "")
        copy_text = entry["post_context"].get("copy", topic)

        # Use notes as the search query directly (user is describing the desired image),
        # fall back to topic if no notes given.
        search_query = notes.strip() if notes else topic
        img = fetch_image(search_query, context=copy_text)

        new_image_path = img.get("image_path", "")
        new_image_url  = img.get("image_url", "")

        # Update both media_context AND post_context image fields so _collect_attachments
        # picks up the new image (it checks post_ctx["image_path"] first).
        media_ctx["image_path"] = new_image_path
        media_ctx["image_url"]  = new_image_url

        post_ctx = dict(entry["post_context"])  # shallow copy — text/hashtags unchanged
        post_ctx["image_path"] = new_image_path
        post_ctx["image_url"]  = new_image_url

        return {
            "post_context":    post_ctx,
            "media_context":   media_ctx,
            "last_score":      entry.get("last_score", 0),
            "score_breakdown": entry.get("score_breakdown", {}),
        }

    # ── Sequential flow ────────────────────────────────────────────────────────

    async def _send_next_pending(self) -> None:
        """Send the next pending post. Called on startup and after each post resolves."""
        queue     = _load_queue()
        all_posts = queue["posts"]
        total     = len(all_posts)
        done      = sum(1 for p in all_posts if p["status"] in {"approved", "discarded"})

        pending = [p for p in all_posts if p["status"] in ("pending", "max_revisions")]
        if not pending:
            return  # _check_done() handles closure

        entry          = pending[0]
        position_label = f"Post {done + 1} of {total}"

        if entry["status"] == "max_revisions":
            await self._resend_max_revisions(entry, position_label)
        else:
            rev = entry.get("revision_count", 0)
            await self._send_draft(
                entry,
                revision_label=f"Revision {rev}" if rev else "",
                position_label=position_label,
            )

    # ── Completion check ───────────────────────────────────────────────────────

    async def _check_done(self):
        """Close the bot if every post is resolved; otherwise advance to the next post."""
        queue    = _load_queue()
        terminal = {"approved", "discarded"}
        if queue["posts"] and all(p["status"] in terminal for p in queue["posts"]):
            log.info("All posts resolved — shutting down")
            self._done_event.set()
            await self.close()
        else:
            await self._send_next_pending()


# ── Public API ─────────────────────────────────────────────────────────────────

async def run_approval_flow(post_entries: list) -> list:
    """
    Submit post entries to Discord for approval. Blocks until every post is
    approved or discarded (or DISCORD_APPROVAL_TIMEOUT seconds elapse).

    Args:
        post_entries: list of dicts built with new_post_entry()

    Returns:
        list of approved post_context dicts
    """
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set in .env / config.py")
    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set in .env / config.py")

    bot = DiscordApprovalBot(post_entries)

    async def _run_bot():
        try:
            await bot.start(DISCORD_BOT_TOKEN)
        except discord.LoginFailure as exc:
            log.error("Discord login failed: %s", exc)
            raise
        except Exception as exc:
            log.error("Bot error: %s", exc)
        finally:
            bot._done_event.set()

    bot_task  = asyncio.create_task(_run_bot())
    done_task = asyncio.create_task(bot._done_event.wait())

    if DISCORD_APPROVAL_TIMEOUT is not None:
        done, pending = await asyncio.wait(
            {done_task, bot_task},
            timeout=DISCORD_APPROVAL_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            log.warning("Approval flow timed out after %ds", DISCORD_APPROVAL_TIMEOUT)
        for t in pending:
            t.cancel()
    else:
        await bot._done_event.wait()
        done_task.cancel()

    if not bot.is_closed():
        await bot.close()

    try:
        await asyncio.wait_for(bot_task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        bot_task.cancel()
        try:
            await bot_task
        except (asyncio.CancelledError, Exception):
            pass

    return bot.approved_posts


def new_post_entry(
    branch: str,
    post_context: dict,
    media_context: dict,
    scheduled_time: str,
    last_score: float = 0.0,
    score_breakdown: Optional[dict] = None,
) -> dict:
    """
    Build a post_entry dict ready for run_approval_flow().

    Args:
        branch:          "A" (project story) or "B" (maintenance tip)
        post_context:    return value of generate_project_story_post() or
                         generate_maintenance_post()
        media_context:   return value of analyze_next_project() (Branch A) or
                         maintenance_context dict from topic_picker (Branch B)
        scheduled_time:  ISO-8601 datetime string for when the post will go live
        last_score:      overall validator score for the current draft
        score_breakdown: per-dimension scores dict from validate_post()["breakdown"]
    """
    return {
        "post_id":            str(uuid.uuid4()),
        "branch":             branch,
        "status":             "pending",
        "revision_count":     0,
        "scheduled_time":     scheduled_time,
        "last_score":         last_score,
        "score_breakdown":    score_breakdown or {},
        "post_context":       post_context,
        "media_context":      media_context,
        "feedback_history":   [],
        "discord_message_id": None,
    }
