import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
ET_RESULTS_URL = "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAST_RESULTS_FILE = os.path.join(DATA_DIR, "last_results.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")

# Public base URL — set BASE_URL in .env for production (e.g. https://your-app.onrender.com)
_port = os.getenv("PORT", "8080")
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{_port}").rstrip("/")
