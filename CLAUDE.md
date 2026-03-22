# PDX Marketing — fb-autoposter

Multi-agent Facebook autoposter for **PDX Remodelling Solutions**, a bathroom renovation contractor in **Toronto, ON (GTA)**. (The "PDX" in the company name is not Portland, OR.)

**Company:** PDX Remodelling Solutions | **Phone:** 437 238 8992 | **Location:** Toronto, ON (GTA)

---

## What this project does

Runs once per week (cron: Monday 8am) to:
1. Analyze before/after project photos and videos from `media/projects/inbox/`
2. Generate Facebook post copy via Claude (2 project stories + 3 maintenance tips = 5 posts/week)
3. Send drafts to a private Discord channel for approval
4. Publish approved posts to the Facebook Page via Graph API (scheduled for their assigned time slots)

---

## Entry point

```bash
cd fb-autoposter
python main.py --dry-run          # generate + print; no Discord or Facebook calls
python main.py                    # full weekly run
python main.py --resend-pending   # re-send pending drafts after a bot crash
python main.py --force-slots 2026-03-17  # use the week of a specific date
```

---

## Project structure

```
fb-autoposter/
├── main.py               # weekly orchestration entry point (Phase 6)
├── config.py             # all config constants + WEEKLY_SCHEDULE
├── discord_bot.py        # Discord approval flow (run_approval_flow, new_post_entry)
├── facebook.py           # Facebook Graph API publisher (post_to_page, prepare_share_kit)
├── queue_manager.py      # post_queue.json read/write + reschedule logic
├── run_log.json          # per-run summary log (array of run objects)
├── post_queue.json       # live post state (pending/approved/scheduled/posted/...)
├── topic_history.json    # 8-week rolling topic history (avoids repeats)
├── media_log.json        # tracks which inbox projects have been used
├── logs/                 # per-run log files: run_YYYY-MM-DD.log
├── media/
│   ├── projects/
│   │   ├── inbox/        # drop new project folders here (before/, after/, video/)
│   │   └── processed/    # projects moved here after analysis
│   ├── trimmed/          # ffmpeg-trimmed video output
│   └── temp/             # Pexels images downloaded per run (auto-cleaned after 7 days)
└── agents/
    ├── generator.py      # generate_project_story_post(), generate_maintenance_post()
    ├── validator.py      # validate_post() → {approved, score, feedback, breakdown}
    ├── media_analyzer.py # analyze_next_project(), analyze_project(), log_used_project()
    ├── video_editor.py   # edit_video() → trims video, returns content_context
    ├── topic_picker.py   # pick_topics(n) → list of post_context dicts
    └── pexels.py         # fetch_image(search_query) → {image_url, image_path, photographer}
```

---

## Phases complete (all 6)

| Phase | What was built |
|-------|---------------|
| 1 | `generator.py` + `validator.py` — Claude generates copy, validator scores 0–10 with retry |
| 2 | `media_analyzer.py` + `video_editor.py` — Claude Vision scores images, ffmpeg trims video |
| 3 | `topic_picker.py` + `pexels.py` + Phase 3 generator functions — weekly tip topics + Pexels images |
| 4 | `discord_bot.py` — full Discord approval loop (approve/reject/remedia/override) |
| 5 | `queue_manager.py` + `facebook.py` — scheduling, reschedule logic, Facebook Graph API |
| 6 | `main.py` — orchestrates all phases, cron setup, CLI flags, run logging |

---

## Weekly schedule slots

| Day       | Time  | Branch |
|-----------|-------|--------|
| Monday    | 10:00 | A (project story) |
| Wednesday | 11:00 | B (maintenance tip) |
| Thursday  | 11:00 | B (maintenance tip) |
| Friday    | 10:00 | A (project story) |
| Saturday  | 12:00 | B (maintenance tip) |

If the inbox has 0 projects, all 5 slots become Branch B. If 1 project, Monday goes to A and Friday flips to B.

---

## Key API signatures

```python
# Generators
generate_project_story_post(media_context: dict) -> dict  # copy, hashtags, media_type, project_name
generate_maintenance_post(maintenance_context: dict) -> dict  # copy, hashtags, topic, image_path, photographer

# Validator
validate_post(post: str) -> dict  # {approved: bool, score: float, feedback: str, breakdown: dict}

# Media
analyze_next_project(move_after_use=False) -> dict | None  # media_context
analyze_project(project_dir) -> dict | None               # media_context
log_used_project(project_name, before_image, after_image, post_id="")

# Topics / Pexels
pick_topics(n=3) -> list[dict]         # returns n post_context dicts
fetch_image(search_query) -> dict      # {image_url, image_path, photographer, pexels_url}

# Queue
add_post(post_context, branch, scheduled_time, media_context=None) -> str  # post_id
update_status(post_id, status, facebook_post_id=None)
get_approved_posts() -> list
check_and_reschedule_approved(post_id) -> str | None  # new_time or None

# Discord
new_post_entry(branch, post_context, media_context, scheduled_time, last_score, score_breakdown) -> dict
run_approval_flow(post_entries: list) -> list  # async; returns approved post_context dicts

# Facebook
post_to_page(post_context, scheduled_time, media_context=None) -> str  # facebook_post_id
prepare_share_kit(post_context) -> dict  # {caption, image_path, video_path, image_url}
```

---

## Config (.env keys required)

```
ANTHROPIC_API_KEY
PEXELS_API_KEY
DISCORD_BOT_TOKEN
DISCORD_CHANNEL_ID
DISCORD_OWNER_ID
FACEBOOK_PAGE_ID
FACEBOOK_ACCESS_TOKEN       # use the never-expire Page token (not the 60-day user token)
FACEBOOK_API_VERSION        # defaults to v19.0
```

---

## Adding new project media

Drop a folder into `media/projects/inbox/` with this structure:
```
inbox/
└── project-name/
    ├── before/   ← JPEG/PNG before photos
    ├── after/    ← JPEG/PNG after photos
    └── video/    ← MP4/MOV (optional; images take priority if both exist)
```

---

## Known behaviour notes

- `discord_bot._check_done()` uses `terminal = {"approved", "discarded"}`. `main.py` prunes resolved posts (`scheduled/posted/failed`) from the queue before each run so the bot can close properly.
- `pick_topics()` handles topic generation + Pexels fetch + post generation internally. `main.py` calls `validate_post()` separately on the returned post_contexts to get scores for `new_post_entry()`.
- Projects are marked as used and moved to `processed/` immediately after `analyze_project()`, not after approval, to prevent reuse if the bot restarts mid-run.
- Facebook publishing is batched: `_publish_approved_posts()` runs after `run_approval_flow()` returns, not inline on each approval. The share kit is still sent to Discord immediately by the bot.
