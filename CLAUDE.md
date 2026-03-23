# PDX Marketing — fb-autoposter

Multi-agent Facebook autoposter for **PDX Remodelling Solutions**, a bathroom renovation contractor in **Toronto, ON (GTA)**. (The "PDX" in the company name is not Portland, OR.)

**Company:** PDX Remodelling Solutions | **Phone:** 437 238 8992 | **Location:** Toronto, ON (GTA)

---

## What this project does

Runs once per week (cron: Monday 7am) to:
1. Analyze before/after project photos and videos from `media/projects/inbox/`
2. Generate Facebook post copy via Claude (2 project stories + 1 maintenance tip + 1 saves post = 4 posts/week)
3. Send drafts to a private Discord channel for approval
4. Publish approved posts to the Facebook Page via Graph API (scheduled for their assigned time slots)

---

## Entry point

```bash
cd fb-autoposter
python main.py                           # full weekly run
python main.py --reset                   # clear logs/temp/history, then run (use for fresh test runs)
python main.py --reset --dry-run         # clear logs/temp/history, generate + print only
python main.py --dry-run                 # generate + print; no Discord or Facebook calls
python main.py --resend-pending          # re-send pending drafts after a bot crash
python main.py --force-slots 2026-03-17  # use the week of a specific date
```

`--reset` clears: `run_log.json`, `topic_history.json`, `media_log.json`, `media/temp/*`

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
    ├── generator.py      # generate_project_story_post(), generate_maintenance_post(), generate_saves_post()
    ├── validator.py      # validate_post() → {approved, score, feedback, breakdown}  (6 dimensions)
    ├── media_analyzer.py # analyze_next_project(), analyze_project(), log_used_project()
    ├── video_editor.py   # edit_video() → trims video, returns content_context
    ├── topic_picker.py   # pick_topics(n) → list of post_context dicts
    └── pexels.py         # fetch_image(search_query, context="") → {image_url, image_path, photographer}
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

4 posts/week. Days and times tuned for peak Facebook engagement (Tue+Sun high-engagement days, 7–9am window).

| Day     | Time  | Branch |
|---------|-------|--------|
| Monday  | 07:30 | A (project story) |
| Tuesday | 08:00 | B (maintenance tip) |
| Friday  | 08:00 | A (project story) |
| Sunday  | 09:00 | C (saves post — cheat sheet / checklist / top-5) |

Sunday (Branch C) is always a saves-optimized post regardless of inbox count.
If the inbox has 0 projects: Mon+Tue+Fri → B, Sun → C. If 1 project: Mon → A, Tue+Fri → B, Sun → C.

---

## Key API signatures

```python
# Generators
generate_project_story_post(media_context: dict) -> dict  # copy, hashtags, media_type, project_name
generate_maintenance_post(maintenance_context: dict) -> dict  # copy, hashtags, topic, image_path
generate_saves_post(topic_context: dict) -> dict  # copy, hashtags, topic, image_path  (Branch C)

# Validator
validate_post(post: str) -> dict  # {approved: bool, score: float, feedback: str, breakdown: dict}

# Media
analyze_next_project(move_after_use=False) -> dict | None  # media_context
analyze_project(project_dir) -> dict | None               # media_context
log_used_project(project_name, before_image, after_image, post_id="")

# Topics / Pexels
pick_topics(n=3) -> list[dict]                          # returns n post_context dicts (copy generated before image fetch)
fetch_image(search_query, context="") -> dict           # {image_url, image_path, photographer, pexels_url}

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
FACEBOOK_ACCESS_TOKEN          # use the never-expire Page token (not the 60-day user token)
FACEBOOK_API_VERSION           # defaults to v19.0
DISCORD_APPROVAL_TIMEOUT       # optional: seconds before bot shuts down waiting (default: wait forever)
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
- `pick_topics()` generates copy **first** (with empty image), then fetches the Pexels image scored against that copy for relevance. `post_angle` is included in the returned dict for Branch C reuse.
- `fetch_image(search_query, context)` passes the post copy to Claude Vision so it picks the photo that best illustrates what the post actually says, not just a generic bathroom photo.
- `remedia` for Branch B/C swaps the image only — copy is never regenerated. Notes typed after `remedia` are used directly as the Pexels search query (e.g. `remedia dirty exhaust fan`).
- `get_season_context()` in `config.py` returns the current Toronto season + weather note; injected into every generator prompt so content stays seasonally relevant.
- Posts are sent to Discord one at a time (sequential flow). After each approve/discard, `_check_done()` advances to the next pending post.
- Discord send failures (e.g. broken pipe) are retried automatically up to 3 times with 2s/4s backoff before giving up.
- Projects are marked as used and moved to `processed/` immediately after `analyze_project()`, not after approval, to prevent reuse if the bot restarts mid-run.
- Facebook publishing is batched: `_publish_approved_posts()` runs after `run_approval_flow()` returns, not inline on each approval.
- After approval, Discord shows only `✅ Scheduled to your Facebook Page — [day date @ time]` — no copy is re-pasted.
