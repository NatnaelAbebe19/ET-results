import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
ET_RESULTS_URL = "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAST_RESULTS_FILE = os.path.join(DATA_DIR, "last_results.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
SUBSCRIBER_PREFS_FILE = os.path.join(DATA_DIR, "subscriber_prefs.json")

# Public base URL — set BASE_URL in .env for production (e.g. https://your-app.onrender.com)
_port = os.getenv("PORT", "8080")
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{_port}").rstrip("/")

# Quiet Hours (pause automated checking from 6 PM to 7 AM East Africa Time)
QUIET_HOURS_ENABLED = os.getenv("QUIET_HOURS_ENABLED", "true").lower() in ("true", "1", "yes")
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "18"))  # 18:00 (6 PM)
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "7"))       # 07:00 (7 AM)
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Africa/Addis_Ababa")  # EAT (UTC+3)
