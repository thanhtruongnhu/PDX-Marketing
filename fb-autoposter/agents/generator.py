"""
generator.py — Facebook post generator for PDX Remodelling Solutions.

Two branches:
  A) "project" — Story post from a media context (before/after photos, videos, etc.)
  B) "tip"     — Home maintenance advice post for Portland homeowners

Calls validator.py after each draft; retries up to MAX_RETRIES times with feedback.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import (
    ANTHROPIC_API_KEY,
    MODEL,
    MAX_TOKENS,
    MAX_RETRIES,
    COMPANY_NAME,
    COMPANY_LOCATION,
    COMPANY_TONE,
    COMPANY_PHONE,
    DUMMY_PROJECT_CONTEXTS,
    DUMMY_TIP_TOPICS,
)
from agents.validator import validate_post

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

PROJECT_STORY_SYSTEM = f"""You write Facebook posts for {COMPANY_NAME}, \
a bathroom renovation company in {COMPANY_LOCATION}.

Tone: {COMPANY_TONE}.

Guidelines:
- 150–250 words (strict — Facebook drops long posts behind "See More")
- Open with a punchy hook about the transformation, NOT with the company name
- Describe what changed and why it matters to the homeowner
- Use 1–3 relevant emojis, naturally placed (not a list of bullet emojis)
- End with a soft CTA like "DM us for a free in-home quote" or "Drop a ❤️ if you love it"
- The very last line of the post must always be: "{COMPANY_NAME} - {COMPANY_PHONE}"
- Do NOT use hashtags in the post body (they go in the first comment)
- Write in first-person plural ("we", "our team") — the company is telling the story"""

TIP_POST_SYSTEM = f"""You write Facebook posts for {COMPANY_NAME}, \
a bathroom renovation company in {COMPANY_LOCATION}.

Tone: {COMPANY_TONE}.

Guidelines:
- 120–200 words
- Open with a relatable hook (a question or a surprising stat works well)
- Give 2–4 specific, actionable tips a homeowner can use today
- Mention {COMPANY_NAME} once at the end, naturally, as the go-to for bigger jobs
- Use 1–2 emojis max
- Do NOT use hashtags in the post body
- Avoid generic advice — GTA homeowners deal with hard winters and humid summers; make it local
- The very last line of the post must always be: "{COMPANY_NAME} - {COMPANY_PHONE}"""


# ---------------------------------------------------------------------------
# Generation helpers
# ---------------------------------------------------------------------------


def _generate(system: str, user_prompt: str) -> str:
    """Single Claude call; returns the text of the first text block."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return next(b.text for b in response.content if b.type == "text").strip()


def _project_prompt(media_context: str, feedback: str) -> str:
    prompt = f"Write a Facebook post about this bathroom project:\n\n{media_context}"
    if feedback:
        prompt += f"\n\nYour previous draft was rejected. Revise it based on this feedback:\n{feedback}"
    return prompt


def _tip_prompt(topic: str, feedback: str) -> str:
    prompt = f"Write a Facebook home maintenance tip post about:\n\n{topic}"
    if feedback:
        prompt += f"\n\nYour previous draft was rejected. Revise it based on this feedback:\n{feedback}"
    return prompt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_post(branch: str, context: str, verbose: bool = True) -> dict:
    """
    Generate and validate a Facebook post with automatic retry on low scores.

    Args:
        branch:  "project" (Branch A) or "tip" (Branch B)
        context: Media context string for "project", topic string for "tip"
        verbose: Print attempt scores to stdout

    Returns:
        {
            "post":     str,    # final post text (best attempt even if never approved)
            "score":    float,  # validator score of the final post
            "attempts": int,    # how many generation attempts were made
            "approved": bool,   # True if any attempt met APPROVAL_THRESHOLD
        }
    """
    if branch not in ("project", "tip"):
        raise ValueError(f"branch must be 'project' or 'tip', got {branch!r}")

    feedback = ""
    last_post = ""
    last_score = 0.0

    for attempt in range(1, MAX_RETRIES + 1):
        if branch == "project":
            post = _generate(PROJECT_STORY_SYSTEM, _project_prompt(context, feedback))
        else:
            post = _generate(TIP_POST_SYSTEM, _tip_prompt(context, feedback))

        result = validate_post(post)
        last_post = post
        last_score = result["score"]

        if verbose:
            status = "✅ APPROVED" if result["approved"] else "❌ rejected"
            print(f"  Attempt {attempt}/{MAX_RETRIES}: score={result['score']:.1f}  {status}")
            if not result["approved"]:
                print(f"  Feedback: {result['feedback']}\n")

        if result["approved"]:
            return {
                "post": post,
                "score": result["score"],
                "attempts": attempt,
                "approved": True,
            }

        feedback = result["feedback"]

    # All retries exhausted — return best (last) attempt
    return {
        "post": last_post,
        "score": last_score,
        "attempts": MAX_RETRIES,
        "approved": False,
    }


# ---------------------------------------------------------------------------
# Phase 3 system prompts
# ---------------------------------------------------------------------------

def _make_project_story_system(media_type: str) -> str:
    """Return a system prompt tuned for image reveals or video reels."""
    if media_type == "video":
        media_note = (
            "You are writing for a short renovation reel — be dynamic and "
            "journey-focused, building toward the reveal moment."
        )
    else:
        media_note = (
            "You are writing for a before/after photo reveal — lead with the "
            "visual contrast and make the reader feel the transformation."
        )

    return f"""You write Facebook posts for {COMPANY_NAME}, \
a bathroom renovation company in {COMPANY_LOCATION}.

Tone: {COMPANY_TONE}.

{media_note}

Post structure (in this order):
1. Hook — one punchy opening line based on the transformation
2. Story — 2–3 sentences with specific visual details from the media: \
tile colour, fixtures, layout change, before condition, after result
3. Subtle company mention — 1 warm sentence, NOT salesy \
(e.g. "Our team loved every minute of this one.")
4. Soft CTA — 1 sentence inviting enquiry \
(e.g. "Thinking about a refresh? Drop us a message — we'd love to hear about your space.")
5. Contact line — always the very last line, exactly: "{COMPANY_NAME} - {COMPANY_PHONE}"

Rules:
- 150–250 words total
- First-person plural only: "our team", "we", "our crew"
- 1–3 emojis naturally placed (not a bullet list of emojis)
- NEVER use: "luxury", "state of the art", "world class", "unbeatable", \
"transform your dreams" — too salesy
- No hashtags inside the post body (they are published separately)
- Be warm and specific — reference what Claude Vision actually saw"""


MAINTENANCE_TIP_SYSTEM = f"""You write Facebook posts for {COMPANY_NAME}, \
a bathroom renovation company in {COMPANY_LOCATION}.

Tone: {COMPANY_TONE}.

Post structure (in this order):
1. Hook — one relatable opening line about the problem or tip
2. Tip — 3–4 sentences of genuinely useful, specific, actionable advice
3. Subtle company mention — 1 sentence only, soft \
(e.g. "If it's past the point of a quick fix, we're always happy to take a look.")
4. Contact line — exactly: "{COMPANY_NAME} - {COMPANY_PHONE}"
5. Photo credit — always the very last line, exactly: "📷 Photo by [name] via Pexels"

Rules:
- 120–200 words total
- GTA homeowners deal with harsh winters, freeze-thaw cycles, and humid summers — \
factor this in where relevant
- Knowledgeable neighbour tone — helpful, not a lecture or a sales pitch
- 1–2 emojis max
- No hashtags inside the post body"""


# ---------------------------------------------------------------------------
# Phase 3 helpers
# ---------------------------------------------------------------------------


def _parse_json_response(raw: str):
    """
    Parse a JSON response that should have "copy" and "hashtags" keys.
    Falls back gracefully if Claude returns plain text instead of JSON.
    Returns (copy: str, hashtags: list[str]).
    """
    clean = raw
    if clean.startswith("```"):
        clean = clean.split("```", 2)[1]
        if clean.startswith("json"):
            clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()
    try:
        data = json.loads(clean)
        return data.get("copy", raw), data.get("hashtags", [])
    except (json.JSONDecodeError, ValueError):
        return raw, []


def _run_with_retry(system: str, build_prompt_fn, verbose: bool = True):
    """
    Run a generation + validation loop up to MAX_RETRIES times.

    build_prompt_fn(feedback: str) -> str  — called each attempt with the
    previous validator feedback (empty string on first attempt).

    Returns (copy: str, hashtags: list[str], approved: bool).
    The copy/hashtags returned are always the best-scoring attempt.
    """
    feedback = ""
    best_copy, best_hashtags, best_score = "", [], 0.0

    for attempt in range(1, MAX_RETRIES + 1):
        raw = _generate(system, build_prompt_fn(feedback))
        copy, hashtags = _parse_json_response(raw)
        result = validate_post(copy)

        if verbose:
            status = "✅ APPROVED" if result["approved"] else "❌ rejected"
            print(f"  Attempt {attempt}/{MAX_RETRIES}: score={result['score']:.1f}  {status}")
            if not result["approved"]:
                print(f"  Feedback: {result['feedback']}\n")

        if result["score"] > best_score:
            best_copy, best_hashtags, best_score = copy, hashtags, result["score"]

        if result["approved"]:
            return copy, hashtags, True

        feedback = result["feedback"]

    return best_copy, best_hashtags, False


# ---------------------------------------------------------------------------
# Phase 3 public API
# ---------------------------------------------------------------------------


def generate_project_story_post(media_context: dict, verbose: bool = True) -> dict:
    """
    Generate a Facebook project story post from a media_context dict.

    Args:
        media_context: Dict from media_analyzer.py:
            {
                "type":            "images" | "video",
                "before_image":    path or None,
                "after_image":     path or None,
                "video_path":      path or None,
                "content_context": str,   ← Claude Vision description
                "project_name":    str,
            }
        verbose: Print attempt scores to stdout

    Returns:
        {
            "copy":         str,
            "hashtags":     list[str],
            "media_type":   "images" | "video",
            "project_name": str,
        }
    """
    media_type = media_context.get("type", "images")
    project_name = media_context.get("project_name", "")
    content_context = media_context.get("content_context", "")

    if media_type == "video":
        intro = (
            "Write a Facebook post narrating this bathroom renovation reel. "
            "Be dynamic and journey-focused — take the reader through the "
            "progress and build to the reveal moment."
        )
    else:
        intro = (
            "Write a Facebook post about this bathroom before/after photo reveal. "
            "Lead with the visual contrast — what changed and why it's striking."
        )

    def build_prompt(feedback: str) -> str:
        prompt = (
            f"{intro}\n\n"
            f"Project: {project_name}\n"
            f"What was seen in the media:\n{content_context}\n\n"
            "Return ONLY valid JSON with exactly two keys:\n"
            '  "copy": the full post text (no hashtags inside)\n'
            '  "hashtags": list of 5–8 hashtag strings; always include '
            '#bathroomrenovation and #beforeandafter, '
            "then 3–6 more based on what was visible in the media\n\n"
            'Example format: {"copy": "post text here", '
            '"hashtags": ["#bathroomrenovation", "#beforeandafter", "#tilesofinstagram"]}'
        )
        if feedback:
            prompt += (
                f"\n\nYour previous draft was rejected. "
                f"Revise based on this feedback:\n{feedback}"
            )
        return prompt

    system = _make_project_story_system(media_type)
    copy, hashtags, _ = _run_with_retry(system, build_prompt, verbose=verbose)

    return {
        "copy": copy,
        "hashtags": hashtags,
        "media_type": media_type,
        "project_name": project_name,
    }


def generate_maintenance_post(maintenance_context: dict, verbose: bool = True) -> dict:
    """
    Generate a Facebook maintenance tip post from a maintenance_context dict.

    Args:
        maintenance_context: Dict from topic_picker.py:
            {
                "topic":        str,
                "post_angle":   str,
                "image_path":   str,
                "image_url":    str,
                "photographer": str,
                "pexels_url":   str,
            }
        verbose: Print attempt scores to stdout

    Returns:
        {
            "copy":         str,
            "hashtags":     list[str],
            "topic":        str,
            "image_path":   str,
            "photographer": str,
        }
    """
    topic = maintenance_context.get("topic", "")
    post_angle = maintenance_context.get("post_angle", "")
    photographer = maintenance_context.get("photographer", "unknown")
    image_path = maintenance_context.get("image_path", "")

    def build_prompt(feedback: str) -> str:
        prompt = (
            f"Write a Facebook home maintenance tip post.\n\n"
            f"Topic: {topic}\n"
            f"Angle: {post_angle}\n\n"
            f"The post body must end with these two lines in this exact order:\n"
            f"{COMPANY_NAME} - {COMPANY_PHONE}\n"
            f"📷 Photo by {photographer} via Pexels\n\n"
            "Return ONLY valid JSON with exactly two keys:\n"
            '  "copy": the full post text including the contact line and photo credit at the end\n'
            '  "hashtags": list of 5–7 hashtag strings; always include '
            "#homeowner, #bathroomtips, and #hometips, "
            "then 2–4 more relevant to the specific topic\n\n"
            f'Example format: {{"copy": "post text here\\n{COMPANY_NAME} - {COMPANY_PHONE}\\n📷 Photo by Jane via Pexels", '
            '"hashtags": ["#homeowner", "#bathroomtips", "#hometips"]}'
        )
        if feedback:
            prompt += (
                f"\n\nYour previous draft was rejected. "
                f"Revise based on this feedback:\n{feedback}"
            )
        return prompt

    copy, hashtags, _ = _run_with_retry(
        MAINTENANCE_TIP_SYSTEM, build_prompt, verbose=verbose
    )

    return {
        "copy": copy,
        "hashtags": hashtags,
        "topic": topic,
        "image_path": image_path,
        "photographer": photographer,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random

    # --- Legacy demo: create_post() with string contexts ---
    print("=" * 60)
    print("Legacy — create_post() Branch A")
    print("=" * 60)
    context_a = random.choice(DUMMY_PROJECT_CONTEXTS)
    print(f"Context:\n{context_a}\n")
    result_a = create_post("project", context_a)
    print(f"\nPost (score {result_a['score']:.1f}, {result_a['attempts']} attempt(s)):\n")
    print(result_a["post"])

    # --- Phase 3 Branch A: images ---
    print("\n" + "=" * 60)
    print("Phase 3 Branch A — generate_project_story_post() [images]")
    print("=" * 60)
    dummy_images_ctx = {
        "type": "images",
        "before_image": "media/projects/inbox/se-portland-master-bath/before/img1.jpg",
        "after_image": "media/projects/inbox/se-portland-master-bath/after/img1.jpg",
        "content_context": (
            "The bathroom was updated from dated pink ceramic tile and avocado-green "
            "fixtures to a modern design with large-format white subway tiles, a floating "
            "double vanity in walnut with undermount sinks, brushed nickel fixtures, "
            "and a frameless glass shower enclosure. The floor now features heated "
            "hex mosaic tile in charcoal grey."
        ),
        "project_name": "se-portland-master-bath",
    }
    result_img = generate_project_story_post(dummy_images_ctx)
    print(f"\n--- Post ---\n{result_img['copy']}")
    print(f"\nHashtags: {' '.join(result_img['hashtags'])}")

    # --- Phase 3 Branch A: video ---
    print("\n" + "=" * 60)
    print("Phase 3 Branch A — generate_project_story_post() [video]")
    print("=" * 60)
    dummy_video_ctx = {
        "type": "video",
        "before_image": None,
        "after_image": None,
        "video_path": "media/projects/inbox/nw-portland-bath-reel/video/clip.mp4",
        "content_context": (
            "The video opens with demo footage of cracked grout and a leaking tub surround, "
            "transitions through tile installation with herringbone penny-round floor tiles "
            "in white and grey, then reveals a complete transformation featuring a "
            "freestanding soaker tub, a custom floating shelf with warm LED strip lighting, "
            "and a large format slate-look porcelain wall tile."
        ),
        "project_name": "nw-portland-bath-reel",
    }
    result_vid = generate_project_story_post(dummy_video_ctx)
    print(f"\n--- Post ---\n{result_vid['copy']}")
    print(f"\nHashtags: {' '.join(result_vid['hashtags'])}")
