"""
validator.py — Critic agent for Facebook post quality control.

Scores posts on: hook strength, clarity, CTA, tone match, length.
Returns: {"approved": bool, "score": float, "feedback": str}
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL, MAX_TOKENS, APPROVAL_THRESHOLD, COMPANY_NAME, COMPANY_LOCATION

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

CRITIC_SYSTEM = f"""You are a tough but fair social media content critic reviewing Facebook posts \
for {COMPANY_NAME}, a bathroom renovation company in {COMPANY_LOCATION}.

Score the post from 0–10 on each criterion below, then set overall_score to the average:

  hook_strength  — Does the opening line grab attention and make you want to keep reading?
  clarity        — Is the message clear, easy to read, and free of jargon?
  cta            — Is there a natural, non-pushy call-to-action (e.g. DM us, comment below)?
  tone_match     — Does it feel like a friendly local contractor, NOT a corporate brand?
  length         — Is it the right length for Facebook (100–280 words)? Penalise anything outside.

Respond ONLY with valid JSON — no markdown fences, no extra text:
{{
  "scores": {{
    "hook_strength": <float 0-10>,
    "clarity":       <float 0-10>,
    "cta":           <float 0-10>,
    "tone_match":    <float 0-10>,
    "length":        <float 0-10>
  }},
  "overall_score": <float 0-10>,
  "feedback": "<one concise paragraph of specific, actionable feedback for the writer>"
}}"""


def validate_post(post: str) -> dict:
    """
    Run the critic Claude against a draft Facebook post.

    Args:
        post: The generated post text to evaluate.

    Returns:
        {
            "approved": bool,   # True if overall_score >= APPROVAL_THRESHOLD
            "score":    float,  # overall_score (0–10)
            "feedback": str,    # actionable revision notes
        }
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=CRITIC_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Evaluate this Facebook post:\n\n{post}",
            }
        ],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()

    # Strip markdown code fences if the model adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

    data = json.loads(raw)

    # Recompute overall_score from sub-scores as a sanity check
    sub_scores = data.get("scores", {})
    if sub_scores:
        computed = sum(sub_scores.values()) / len(sub_scores)
        overall = round((float(data.get("overall_score", computed)) + computed) / 2, 2)
    else:
        overall = float(data.get("overall_score", 0))

    return {
        "approved":  overall >= APPROVAL_THRESHOLD,
        "score":     overall,
        "feedback":  data.get("feedback", ""),
        "breakdown": sub_scores,   # {"hook_strength": float, "clarity": float, ...}
    }
