"""
video_editor.py — Trims a project video to the optimal length for Facebook.

Claude Vision analyzes sample frames to understand the content type, then
ffmpeg trims the video accordingly:

    Before/after reveal → 15–30 sec
    Project walkthrough → 30–60 sec
    Work in progress    → 30–45 sec
    Unknown/mixed       → 30 sec default

No captions, overlays, or re-encoding — trim only (stream copy).

Returns:
    {
        "output_path":    str,   # path to trimmed video
        "content_context": str,  # Claude's description of what it saw
    }
"""

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile  # still used by _extract_frames
from pathlib import Path
from typing import Union

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL

log = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_HERE = Path(__file__).resolve().parent.parent
OUTPUT_DIR = _HERE / "media" / "trimmed"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Content-type → trim window (seconds)
# ---------------------------------------------------------------------------

TRIM_RULES = {
    "before_after": (15, 30),
    "walkthrough": (30, 60),
    "work_in_progress": (30, 45),
    "unknown": (30, 30),
}

# ---------------------------------------------------------------------------
# ffprobe / ffmpeg helpers
# ---------------------------------------------------------------------------


def _get_duration(video_path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def _extract_frames(video_path: Path, n: int = 4) -> list[tuple[str, str]]:
    """
    Extract n evenly-spaced frames from the video as base64 JPEGs.
    Returns list of (base64_data, "image/jpeg") tuples.
    """
    duration = _get_duration(video_path)
    frames = []

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = duration * (i + 1) / (n + 1)
            out = Path(tmp) / f"frame_{i:02d}.jpg"
            cmd = [
                "ffmpeg",
                "-ss", str(t),
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(out),
                "-y",
                "-loglevel", "error",
            ]
            subprocess.run(cmd, check=True)
            if out.exists():
                data = base64.standard_b64encode(out.read_bytes()).decode()
                frames.append((data, "image/jpeg"))

    return frames


def _analyze_frames(frames: list[tuple[str, str]], video_path: Path) -> dict:
    """
    Send frames to Claude Vision and get back content_type + description.

    Returns:
        {
            "content_type": "before_after" | "walkthrough" | "work_in_progress" | "unknown",
            "description":  str,
        }
    """
    content_blocks = []
    for i, (data, media_type) in enumerate(frames):
        content_blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        )
        content_blocks.append(
            {"type": "text", "text": f"Frame {i + 1} of {len(frames)}."}
        )

    content_blocks.append(
        {
            "type": "text",
            "text": (
                f"These {len(frames)} frames are evenly spaced throughout a bathroom renovation video "
                f"from Portland, OR contractor PDX Remodelling Solutions.\n\n"
                "1. Classify the video content type as one of:\n"
                "   - before_after  (shows a clear before state and/or after reveal)\n"
                "   - walkthrough   (tours a completed renovation)\n"
                "   - work_in_progress (shows active construction or mid-renovation state)\n"
                "   - unknown       (doesn't fit the above)\n\n"
                "2. Write 2–4 sentences describing what you see: specific materials, fixtures, "
                "colors, tiles, and design choices visible in the frames. Be concrete — this "
                "description will be used to write a Facebook post.\n\n"
                "Respond ONLY with valid JSON, no extra text:\n"
                '{"content_type": "<type>", "description": "<description>"}'
            ),
        }
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": content_blocks}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

    return json.loads(raw)


def _trim_video(
    video_path: Path,
    duration: float,
    target_min: int,
    target_max: int,
) -> Path:
    """
    Trim video to fit within [target_min, target_max] seconds.

    Strategy:
    - If duration already fits, copy the full file.
    - If duration > target_max, trim from the start to target_max.
    - Uses stream copy (no re-encode) for speed and quality.

    Returns path to the trimmed file (in a temp dir).
    """
    trim_len = min(float(target_max), duration)
    # If video is already short enough, no trimming needed
    if duration <= target_max:
        trim_len = duration

    suffix = video_path.suffix
    out_path = OUTPUT_DIR / f"{video_path.stem}_trimmed{suffix}"

    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-t", str(trim_len),
        "-c", "copy",   # stream copy — no re-encode
        str(out_path),
        "-y",
        "-loglevel", "error",
    ]
    subprocess.run(cmd, check=True)
    log.info("Trimmed to %.1f s → %s", trim_len, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def edit_video(video_path: Union[Path, str]) -> dict:
    """
    Analyze and trim a project video.

    Args:
        video_path: Path to the input video file.

    Returns:
        {
            "output_path":     str,   # absolute path to trimmed video
            "content_context": str,   # Claude's description (for generator.py)
        }
    """
    video_path = Path(video_path)
    log.info("Analyzing video: %s", video_path.name)

    duration = _get_duration(video_path)
    log.info("Duration: %.1f s", duration)

    frames = _extract_frames(video_path, n=4)
    if not frames:
        raise RuntimeError(f"Could not extract frames from {video_path}")

    analysis = _analyze_frames(frames, video_path)
    content_type = analysis.get("content_type", "unknown")
    description = analysis.get("description", "")

    log.info("Content type detected: %s", content_type)
    log.info("Description: %s", description)

    target_min, target_max = TRIM_RULES.get(content_type, TRIM_RULES["unknown"])
    log.info("Trim target: %d–%d s", target_min, target_max)

    out_path = _trim_video(video_path, duration, target_min, target_max)

    return {
        "output_path": str(out_path),
        "content_context": description,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 -m agents.video_editor <path_to_video>")
        sys.exit(1)

    result = edit_video(sys.argv[1])
    print("\n=== video_editor result ===")
    print(f"  output_path:     {result['output_path']}")
    print(f"  content_context: {result['content_context']}")
