import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _parse_channel_ids(raw: str) -> list[int | str]:
    """Parse comma-separated channel IDs: '@public_channel' or numeric '-100123...' IDs."""
    ids: list[int | str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.lstrip("-").isdigit():
            ids.append(int(part))
        else:
            ids.append(part if part.startswith("@") else f"@{part}")
    return ids


# Telegram Channel(s) to auto-post new results to (bot must be an admin of the channel).
# Comma-separated: @my_public_channel or -1001234567890 (private channel ID)
TELEGRAM_CHANNEL_IDS = _parse_channel_ids(os.getenv("TELEGRAM_CHANNEL_IDS", ""))
# Which results are posted to the channel(s): all | pilot | cabin | amt | aviation
TELEGRAM_CHANNEL_ROLE_FILTER = os.getenv("TELEGRAM_CHANNEL_ROLE_FILTER", "all").strip().lower()
DATABASE_URL = os.getenv("DATABASE_URL")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
ET_RESULTS_URL = "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAST_RESULTS_FILE = os.path.join(DATA_DIR, "last_results.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
SUBSCRIBER_PREFS_FILE = os.path.join(DATA_DIR, "subscriber_prefs.json")
DISCORD_SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "discord_subscribers.json")

# Public base URL — set BASE_URL in .env for production (e.g. https://your-app.onrender.com)
_port = os.getenv("PORT", "8080")
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{_port}").rstrip("/")

# Quiet Hours (pause automated checking from 6 PM to 7 AM East Africa Time)
QUIET_HOURS_ENABLED = os.getenv("QUIET_HOURS_ENABLED", "true").lower() in ("true", "1", "yes")
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "18"))  # 18:00 (6 PM)
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "7"))       # 07:00 (7 AM)
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Africa/Addis_Ababa")  # EAT (UTC+3)

# Discord Interactive Bot (optional — runs alongside Telegram with interactive filters)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# Discord Webhook Notifications (optional — runs alongside Telegram / Discord Bot)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ROLE_FILTER = os.getenv("DISCORD_ROLE_FILTER", "all").strip().lower()
