"""
media_analyzer.py — Scans inbox for project media and selects the best images/video.

For image projects: Claude Vision scores each before/ and after/ image independently.
Returns the best before + after pair.

For video projects: delegates to video_editor.py for trimming and context extraction.

Images take priority if both images and video exist.

Returns a media_context dict:
    {
        "type":            "images" | "video",
        "before_image":    path or None,
        "after_image":     path or None,
        "video_path":      path or None,
        "content_context": str,
        "project_name":    str,
    }
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent
INBOX_DIR = _HERE / "media" / "projects" / "inbox"
PROCESSED_DIR = _HERE / "media" / "projects" / "processed"
MEDIA_LOG = _HERE / "media_log.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Media log helpers
# ---------------------------------------------------------------------------


def _load_log() -> dict:
    if MEDIA_LOG.exists():
        return json.loads(MEDIA_LOG.read_text())
    return {"used_projects": []}


def _save_log(data: dict) -> None:
    MEDIA_LOG.write_text(json.dumps(data, indent=2))


def _already_used(project_name: str) -> bool:
    log_data = _load_log()
    return project_name in {e["project"] for e in log_data.get("used_projects", [])}


def log_used_project(
    project_name: str,
    before_image: str | None,
    after_image: str | None,
    post_id: str = "",
) -> None:
    """Record a project as used in media_log.json."""
    log_data = _load_log()
    log_data.setdefault("used_projects", []).append(
        {
            "project": project_name,
            "before_image": before_image,
            "after_image": after_image,
            "used_on": str(date.today()),
            "post_id": post_id,
        }
    )
    _save_log(log_data)


# ---------------------------------------------------------------------------
# Claude Vision helpers
# ---------------------------------------------------------------------------


def _encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a local image file."""
    ext = path.suffix.lower()
    media_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_map.get(ext, "image/jpeg")
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return data, media_type


def _score_image(path: Path, role: str) -> float:
    """
    Ask Claude Vision to score one image on a 0–10 scale.

    role: "before" or "after"
    Scores: contrast_clarity, lighting_quality, visual_impact
    """
    b64, media_type = _encode_image(path)

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
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is a '{role}' photo from a bathroom renovation project. "
                            "Score it from 0–10 on these three criteria:\n"
                            "  contrast_clarity  — sharpness and contrast between surfaces\n"
                            "  lighting_quality  — how well-lit and evenly exposed the image is\n"
                            "  visual_impact     — how striking and compelling it looks for social media\n\n"
                            "Respond ONLY with valid JSON, no extra text:\n"
                            '{"contrast_clarity": <float>, "lighting_quality": <float>, "visual_impact": <float>}'
                        ),
                    },
                ],
            }
        ],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

    scores = json.loads(raw)
    return round(sum(scores.values()) / len(scores), 2)


def _pick_best_image(images: list[Path], role: str) -> tuple[Path, float]:
    """Score all images and return (best_path, best_score)."""
    scored = []
    for img in images:
        try:
            score = _score_image(img, role)
            log.info("  %s — %s score: %.1f", img.name, role, score)
            scored.append((img, score))
        except Exception as exc:
            log.warning("  Could not score %s: %s", img.name, exc)

    if not scored:
        raise ValueError(f"No scoreable {role} images found.")

    return max(scored, key=lambda t: t[1])


# ---------------------------------------------------------------------------
# Inbox scanner
# ---------------------------------------------------------------------------


def _list_project_folders() -> list[Path]:
    """Return all immediate subdirectories of inbox/ that haven't been used."""
    if not INBOX_DIR.exists():
        return []
    folders = [p for p in INBOX_DIR.iterdir() if p.is_dir()]
    unused = [f for f in folders if not _already_used(f.name)]
    return sorted(unused)


def _has_images(project: Path) -> tuple[list[Path], list[Path]]:
    """Return (before_images, after_images) lists."""
    before = sorted(
        p for p in (project / "before").glob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ) if (project / "before").exists() else []
    after = sorted(
        p for p in (project / "after").glob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ) if (project / "after").exists() else []
    return before, after


def _has_video(project: Path) -> list[Path]:
    """Return list of video files in project/video/."""
    video_dir = project / "video"
    if not video_dir.exists():
        return []
    return sorted(
        p for p in video_dir.glob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _analyze_project_dir(project: Path) -> dict | None:
    """
    Core analysis for a single project directory. Returns a media_context dict
    or None if no usable media is found. Called by both analyze_next_project()
    and analyze_project().
    """
    project_name = project.name
    log.info("Analyzing project: %s", project_name)

    before_imgs, after_imgs = _has_images(project)
    videos = _has_video(project)

    media_context: dict = {
        "type": None,
        "before_image": None,
        "after_image": None,
        "video_path": None,
        "content_context": "",
        "project_name": project_name,
        "project_dir": str(project),   # kept for remedia re-analysis
    }

    # --- Image branch (takes priority) ---
    if before_imgs and after_imgs:
        log.info("Image project detected (%d before, %d after images)", len(before_imgs), len(after_imgs))

        best_before, score_b = _pick_best_image(before_imgs, "before")
        best_after, score_a = _pick_best_image(after_imgs, "after")

        log.info("Selected before: %s (%.1f)", best_before.name, score_b)
        log.info("Selected after:  %s (%.1f)", best_after.name, score_a)

        b64_b, mt_b = _encode_image(best_before)
        b64_a, mt_a = _encode_image(best_after)

        ctx_response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mt_b, "data": b64_b},
                        },
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mt_a, "data": b64_a},
                        },
                        {
                            "type": "text",
                            "text": (
                                "The first image is the BEFORE photo and the second is the AFTER photo "
                                "from a bathroom renovation. Write 2–3 sentences describing what changed: "
                                "specific materials, fixtures, colors, and design choices visible in the images. "
                                "Be concrete and specific — this description will be used to write a social media post."
                            ),
                        },
                    ],
                }
            ],
        )

        content_context = next(
            b.text for b in ctx_response.content if b.type == "text"
        ).strip()

        media_context.update(
            {
                "type": "images",
                "before_image": str(best_before),
                "after_image": str(best_after),
                "content_context": content_context,
            }
        )

        if videos:
            log.info("Video also present in project — skipping (images take priority).")

    # --- Video branch ---
    elif videos:
        log.info("Video project detected (%d video(s))", len(videos))
        from agents.video_editor import edit_video

        video_result = edit_video(videos[0])
        media_context.update(
            {
                "type": "video",
                "video_path": video_result["output_path"],
                "content_context": video_result["content_context"],
            }
        )

    else:
        log.warning(
            "Project '%s' has no usable before+after images or videos — skipping.",
            project_name,
        )
        return None

    return media_context


def analyze_project(project_dir: str | Path) -> dict | None:
    """
    Analyze a specific project directory by path.
    Used by the remedia revision flow in discord_bot.py.

    Returns media_context dict or None if no usable media found.
    """
    return _analyze_project_dir(Path(project_dir))


def analyze_next_project(move_after_use: bool = False) -> dict | None:
    """
    Find the next unprocessed project in inbox/ and build a media_context dict.

    Args:
        move_after_use: If True, move the project folder to processed/ and log it.
                        Set to True only after the post has been approved & published.

    Returns:
        media_context dict or None if inbox is empty.
    """
    folders = _list_project_folders()

    if not folders:
        log.warning("Inbox is empty — no projects to process.")
        return None

    if len(folders) == 1:
        log.warning(
            "Low media stock: only 1 project left in inbox after this one. "
            "Add more project folders soon."
        )

    project = folders[0]
    media_context = _analyze_project_dir(project)

    if media_context is None:
        return None

    # --- Low-stock check after selection ---
    remaining = [f for f in _list_project_folders() if f != project]
    if len(remaining) == 0:
        log.warning("Inbox will be EMPTY after this project is used. Add more content.")
    elif len(remaining) == 1:
        log.warning("Only 1 project will remain in inbox after this one. Restock soon.")

    if move_after_use:
        project_name = project.name
        dest = PROCESSED_DIR / project_name
        shutil.move(str(project), str(dest))
        log.info("Moved '%s' → processed/", project_name)
        log_used_project(
            project_name,
            media_context["before_image"],
            media_context["after_image"],
        )

    return media_context


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = analyze_next_project(move_after_use=False)
    if result:
        print("\n=== media_context ===")
        for k, v in result.items():
            if k == "content_context":
                print(f"  {k}:\n    {v}")
            else:
                print(f"  {k}: {v}")
    else:
        print("No projects found in inbox.")
