"""
facebook.py — Facebook Page publisher for PDX Remodelling Solutions fb-autoposter.

Publishes or schedules approved posts via the Facebook Graph API and
generates the community-group share kit for manual posting.

Public API:
    post_to_page(post_context, scheduled_time, media_context=None) → facebook_post_id
    prepare_share_kit(post_context) → dict
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(__file__))

from config import (
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    FACEBOOK_ACCESS_TOKEN,
    FACEBOOK_API_VERSION,
    FACEBOOK_PAGE_ID,
)

log = logging.getLogger(__name__)

_BASE_URL = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}"

# Facebook error codes that indicate an expired / invalid token
_TOKEN_ERROR_CODES = {100, 102, 190, 200, 210, 270}


# ── Discord notification helper ────────────────────────────────────────────────

def _notify_discord(message: str) -> None:
    """Send a plain text message to the configured Discord channel via REST."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        log.warning("Discord not configured — skipping notification: %s", message)
        return
    try:
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


# ── Facebook API helpers ───────────────────────────────────────────────────────

def _parse_fb_error(response: requests.Response, context: str) -> str:
    """Log and return a human-readable Facebook error message."""
    try:
        data  = response.json()
        error = data.get("error", {})
        code  = error.get("code", response.status_code)
        msg   = error.get("message", response.text[:300])
    except Exception:
        code = response.status_code
        msg  = response.text[:300]

    if code in _TOKEN_ERROR_CODES:
        err = (
            "Facebook access token has expired. Generate a new long-lived token at "
            "developers.facebook.com and update FACEBOOK_ACCESS_TOKEN in config.py"
        )
        log.error("%s — %s", context, err)
        _notify_discord(f"⚠️ {err}")
        return err

    err = f"[{code}] {msg}"
    log.error("%s — API error: %s", context, err)
    return err


def _fb_post(endpoint: str, data: Optional[dict] = None, files: Optional[dict] = None) -> dict:
    """
    POST to the Facebook Graph API with one automatic retry on rate limit.

    Args:
        endpoint: path after the base URL, e.g. "{page_id}/feed"
        data:     form fields dict
        files:    multipart files dict (for binary uploads)

    Returns:
        Parsed JSON response dict.

    Raises:
        requests.HTTPError on non-recoverable failure.
    """
    url    = f"{_BASE_URL}/{endpoint}"
    params = {"access_token": FACEBOOK_ACCESS_TOKEN}

    for attempt in range(2):
        resp = requests.post(url, params=params, data=data, files=files, timeout=60)

        if resp.status_code == 200:
            return resp.json()

        # Check for rate-limit error codes
        try:
            error_code = resp.json().get("error", {}).get("code")
        except Exception:
            error_code = None

        if error_code in (4, 17, 32, 613) and attempt == 0:
            log.warning("Facebook rate limit hit — waiting 60 s before retry …")
            time.sleep(60)
            continue

        err_msg = _parse_fb_error(resp, endpoint)
        raise requests.HTTPError(err_msg, response=resp)

    raise requests.HTTPError("Facebook API: max retries exceeded")


def _upload_unpublished_photo(
    image_path: Optional[str] = None,
    image_url: Optional[str] = None,
) -> str:
    """
    Upload a photo as unpublished (to be attached in a multi-photo feed post).

    Returns:
        photo_id string
    """
    payload = {"published": "false"}

    if image_path:
        with open(image_path, "rb") as f:
            result = _fb_post(
                f"{FACEBOOK_PAGE_ID}/photos",
                data=payload,
                files={"source": f},
            )
    elif image_url:
        payload["url"] = image_url
        result = _fb_post(f"{FACEBOOK_PAGE_ID}/photos", data=payload)
    else:
        raise ValueError("Either image_path or image_url must be provided")

    return result["id"]


# ── Public API ─────────────────────────────────────────────────────────────────

def post_to_page(
    post_context: dict,
    scheduled_time: str,
    media_context: Optional[dict] = None,
) -> str:
    """
    Publish or schedule a post to the Facebook Page.

    If scheduled_time is in the future: uses Facebook's native scheduled publish
    (published=false + scheduled_publish_time).
    If scheduled_time is in the past (defensive fallback): publishes immediately.

    Args:
        post_context:    post_context dict from the queue entry
        scheduled_time:  ISO-8601 string for intended publish time
        media_context:   optional media_context dict (used for before/after paths
                         when they are not already in post_context)

    Returns:
        facebook_post_id string

    Raises:
        requests.HTTPError on API failure (also notifies Discord).
    """
    # Build full message text (copy + hashtags)
    copy     = post_context.get("copy", "")
    hashtags = post_context.get("hashtags", [])
    message  = copy + ("\n\n" + " ".join(hashtags) if hashtags else "")

    # Determine scheduling vs immediate publish
    try:
        sched_dt = datetime.fromisoformat(scheduled_time)
    except (ValueError, TypeError):
        sched_dt = None

    is_future = sched_dt is not None and sched_dt > datetime.now()

    def _sched_fields() -> dict:
        """Return scheduling payload fields for future posts."""
        if is_future and sched_dt:
            return {
                "published":              "false",
                "scheduled_publish_time": str(int(sched_dt.timestamp())),
            }
        return {"published": "true"}

    media_type = post_context.get("media_type", "single_image")
    mc = media_context or {}

    try:
        # ── Branch A: before + after images ───────────────────────────────────
        if media_type == "images":
            before = post_context.get("before_image") or mc.get("before_image")
            after  = post_context.get("after_image")  or mc.get("after_image")

            if not before and not after:
                log.warning(
                    "Images post has no image paths — falling back to text-only post"
                )
                return _post_text_only(message, _sched_fields())

            photo_ids = []
            if before:
                photo_ids.append(_upload_unpublished_photo(image_path=before))
            if after:
                photo_ids.append(_upload_unpublished_photo(image_path=after))

            payload = {
                "message":        message,
                "attached_media": json.dumps([{"media_fbid": pid} for pid in photo_ids]),
            }
            payload.update(_sched_fields())
            result = _fb_post(f"{FACEBOOK_PAGE_ID}/feed", data=payload)
            fb_id = result["id"]

        # ── Branch A: video ───────────────────────────────────────────────────
        elif media_type == "video":
            video_path = post_context.get("video_path") or mc.get("video_path")

            if not video_path:
                log.warning(
                    "Video post has no video path — falling back to text-only post"
                )
                return _post_text_only(message, _sched_fields())

            payload = {"description": message}
            payload.update(_sched_fields())

            with open(video_path, "rb") as f:
                result = _fb_post(
                    f"{FACEBOOK_PAGE_ID}/videos",
                    data=payload,
                    files={"source": f},
                )
            fb_id = result["id"]

        # ── Branch B: single image (Pexels URL or local path) ─────────────────
        else:  # "single_image" or anything else
            image_url  = post_context.get("image_url")
            image_path = post_context.get("image_path")

            if image_url:
                payload = {"caption": message, "url": image_url}
                payload.update(_sched_fields())
                result = _fb_post(f"{FACEBOOK_PAGE_ID}/photos", data=payload)
                fb_id  = result["id"]

            elif image_path:
                payload = {"caption": message}
                payload.update(_sched_fields())
                with open(image_path, "rb") as f:
                    result = _fb_post(
                        f"{FACEBOOK_PAGE_ID}/photos",
                        data=payload,
                        files={"source": f},
                    )
                fb_id = result["id"]

            else:
                log.warning(
                    "Single-image post has no image URL or path — posting text only"
                )
                fb_id = _post_text_only(message, _sched_fields())

        action = "scheduled" if is_future else "published immediately"
        log.info("Post %s to Facebook (%s)  fb_id=%s", action, media_type, fb_id)
        return fb_id

    except requests.HTTPError as exc:
        topic_or_project = (
            post_context.get("project_name") or post_context.get("topic", "Unknown")
        )
        fail_msg = (
            f"❌ Failed to {'schedule' if is_future else 'publish'} "
            f"**{topic_or_project}** to Facebook.\n"
            f"Error: {exc}\n"
            "You may need to post this one manually."
        )
        _notify_discord(fail_msg)
        raise


def _post_text_only(message: str, sched_fields: dict) -> str:
    """Post text-only to the page feed. Returns facebook_post_id."""
    payload = {"message": message}
    payload.update(sched_fields)
    result = _fb_post(f"{FACEBOOK_PAGE_ID}/feed", data=payload)
    return result["id"]


def prepare_share_kit(post_context: dict) -> dict:
    """
    Build the community-group share kit dict — no API calls.

    Packages the post copy + hashtags along with media references so
    discord_bot.py can format and send the share-kit message for manual
    posting in community groups.

    Returns:
        {
            "caption":    full post copy + hashtags as a single string,
            "image_path": local after-image path or None,
            "video_path": local video path or None,
            "image_url":  Pexels URL or None,
        }
    """
    copy     = post_context.get("copy", "")
    hashtags = post_context.get("hashtags", [])
    caption  = copy + ("\n\n" + " ".join(hashtags) if hashtags else "")

    # Prefer after_image over image_path for Branch A share kits
    image_path = (
        post_context.get("after_image")
        or post_context.get("image_path")
    )

    return {
        "caption":    caption,
        "image_path": image_path,
        "video_path": post_context.get("video_path"),
        "image_url":  post_context.get("image_url"),
    }
