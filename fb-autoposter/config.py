import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DISCORD_OWNER_ID   = int(os.getenv("DISCORD_OWNER_ID", "0"))
_timeout_env = os.getenv("DISCORD_APPROVAL_TIMEOUT", "").strip()
DISCORD_APPROVAL_TIMEOUT = int(_timeout_env) if _timeout_env else None  # None = wait forever

MODEL = "claude-opus-4-6"
MAX_TOKENS = 1024

APPROVAL_THRESHOLD = 7.5
MAX_RETRIES = 3

COMPANY_NAME = "PDX Remodelling Solutions"
COMPANY_LOCATION = "Toronto, ON (GTA)"
COMPANY_TONE = "friendly, conversational, proud local GTA contractor — not corporate"
COMPANY_PHONE = "437 238 8992"

# Dummy media contexts for Phase 1 (real media injected in Phase 2)
DUMMY_PROJECT_CONTEXTS = [
    (
        "Before/after photos of a master bath renovation in Mississauga. "
        "Walk-in shower with white subway tile and a frameless glass door, "
        "double vanity with quartz countertop, oil-rubbed bronze fixtures, and new heated tile floors."
    ),
    (
        "Time-lapse video of a half-bath powder room makeover in North York. "
        "Wall-mounted floating sink, bold botanical wallpaper, matte black fixtures, "
        "custom round mirror with backlit LED surround."
    ),
    (
        "Photo gallery of a hall bathroom remodel in Oakville. "
        "New soaker tub replacing an old shower-tub combo, herringbone floor tile, "
        "shiplap accent wall, updated vanity lighting, and a large privacy window."
    ),
]

# ── Facebook ──────────────────────────────────────────────────────────────────
FACEBOOK_PAGE_ID      = os.getenv("FACEBOOK_PAGE_ID", "")
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "")
FACEBOOK_API_VERSION  = os.getenv("FACEBOOK_API_VERSION", "v19.0")

# ── Video ─────────────────────────────────────────────────────────────────────
VIDEO_MAX_SECONDS = 60  # hard ceiling for all trimmed videos (Facebook Reels max)

# ── Weekly post schedule ──────────────────────────────────────────────────────
# 4 slots per week: A = project story, B = maintenance tip, C = saves-optimized post
# Days/times tuned for peak Facebook engagement (Tue+Sun top days, 7–9am window)
WEEKLY_SCHEDULE = [
    {"day": "Monday",   "time": "07:30", "branch": "A"},
    {"day": "Tuesday",  "time": "08:00", "branch": "B"},
    {"day": "Friday",   "time": "08:00", "branch": "A"},
    {"day": "Sunday",   "time": "09:00", "branch": "C"},
]

def get_season_context() -> str:
    """Return current season + Toronto weather note for injection into generation prompts."""
    month = date.today().month
    if month in (12, 1, 2):
        return (
            "Current season: Winter (Toronto — freezing temps, ice, dry indoor air, "
            "burst-pipe risk, freeze-thaw cycles stressing grout and caulk)"
        )
    elif month in (3, 4, 5):
        return (
            "Current season: Spring (Toronto — snowmelt and rain increase moisture and "
            "flood risk; cold nights still possible; ideal time to inspect waterproofing)"
        )
    elif month in (6, 7, 8):
        return (
            "Current season: Summer (Toronto — high humidity, mold and mildew growth, "
            "AC condensation, hard water buildup; ventilation becomes critical)"
        )
    else:
        return (
            "Current season: Fall (Toronto — time to prep for winter; caulking, grout "
            "sealing, exhaust fans, and draft-proofing before the cold sets in)"
        )


DUMMY_TIP_TOPICS = [
    "How to regrout shower tile to stop water damage before it starts",
    "When to replace caulk around a bathtub or shower — and how to do it yourself",
    "How to clean a showerhead and remove mineral buildup without harsh chemicals",
    "Five signs your bathroom needs a professional renovation (not just a refresh)",
    "Easy weekend bathroom updates that actually add resale value to your GTA home",
]
