"""
Ethiopian Airlines Results Checker Bot — Main Entry Point.

Runs the Telegram bot alongside a periodic scraper that checks for
new results every N minutes and sends notifications to subscribers.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from config import (
    CHECK_INTERVAL_SECONDS,
    DATA_DIR,
    LAST_RESULTS_FILE,
    TELEGRAM_BOT_TOKEN,
)
from scraper import fetch_results
from bot import build_application, load_subscribers, send_notification

# ─── Logging Setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("et_results_bot")


# ─── State Management ───────────────────────────────────────────────────────

def load_last_results() -> dict[str, dict]:
    """Load previously seen results from disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LAST_RESULTS_FILE):
        try:
            with open(LAST_RESULTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Convert list to dict keyed by id for fast lookup
                if isinstance(data, list):
                    return {r["id"]: r for r in data}
                return data
        except (json.JSONDecodeError, TypeError, KeyError):
            return {}
    return {}


def save_last_results(results: dict[str, dict]):
    """Save current results to disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LAST_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


# ─── Periodic Check Job ─────────────────────────────────────────────────────

async def check_for_new_results(app: Application):
    """
    Scheduled job: scrape the results page, compare with saved state,
    and send notifications for any new entries.
    """
    logger.info("🔍 Checking for new results...")

    current_results = fetch_results()
    if not current_results:
        logger.warning("No results fetched (page might be down). Skipping.")
        return

    last_results = load_last_results()
    new_results = []

    for result in current_results:
        if result["id"] not in last_results:
            new_results.append(result)

    if new_results:
        logger.info(f"🆕 Found {len(new_results)} NEW result(s)!")
        subscribers = load_subscribers()

        if not subscribers:
            logger.warning("No subscribers yet. New results won't be sent to anyone.")
        else:
            for result in new_results:
                for chat_id in subscribers:
                    await send_notification(app, chat_id, result)
                    await asyncio.sleep(0.3)  # Rate limiting

        # Update saved state with ALL current results
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        logger.info(f"✅ State updated. Total results tracked: {len(all_results)}")
    else:
        logger.info("No new results found.")
        # Still update the state in case results were removed
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)


# ─── Health Check Server ───────────────────────────────────────────────────

async def handle_health_check(request):
    """Simple health check endpoint for hosting providers."""
    return web.Response(text="Bot is running! ✈️")


async def start_web_server():
    """Start a dummy web server to keep the bot alive on free hosting."""
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Get port from environment variable (standard for cloud hosting)
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    logger.info(f"🕸️ Health check server starting on port {port}...")
    await site.start()


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    """Start the bot and scheduler."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set in .env file!")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  Ethiopian Airlines Results Checker Bot")
    logger.info(f"  Check interval: every {CHECK_INTERVAL_SECONDS} seconds")
    logger.info("=" * 60)

    # Build the Telegram bot application
    app = build_application()

    # Initialize the bot
    await app.initialize()
    await app.start()

    # Start polling for Telegram messages
    await app.updater.start_polling(drop_pending_updates=True)

    # Start the web server for free hosting
    await start_web_server()

    logger.info("✅ Bot is running! Send /start to your bot on Telegram.")

    # Set up the periodic scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_for_new_results,
        "interval",
        seconds=CHECK_INTERVAL_SECONDS,
        args=[app],
        id="check_results",
        name="Check ET Results",
        max_instances=1,
    )
    scheduler.start()

    # Run an initial check right away
    logger.info("Running initial check...")
    await check_for_new_results(app)

    logger.info(f"⏰ Next check in {CHECK_INTERVAL_SECONDS} seconds. Press Ctrl+C to stop.")

    # Keep running until interrupted
    stop_event = asyncio.Event()

    def signal_handler(*_):
        logger.info("\n🛑 Shutting down...")
        stop_event.set()

    # Handle both SIGINT and SIGTERM
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    else:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("👋 Bot stopped. Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
