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
from result_parser import save_result

# ─── Logging Setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("et_results_bot")

# ─── In-Memory Sent Guard ────────────────────────────────────────────────────
# Tracks result IDs already sent this session to prevent duplicate notifications
# even if the file state hasn't been flushed yet.
_already_notified: set[str] = set()


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

def seed_initial_state():
    """
    If there is no saved state (first run / fresh deploy), scrape the page
    once and save all current results WITHOUT sending any notifications.
    This prevents spamming users with all existing results on startup.
    Also pre-populates the in-memory sent guard so existing results are
    never treated as new even if the file state is stale.
    """
    global _already_notified
    last_results = load_last_results()
    if last_results:
        logger.info(f"📂 Found existing state with {len(last_results)} tracked results.")
        # Pre-populate the in-memory guard from saved state
        _already_notified.update(last_results.keys())
        return  # Already have state, nothing to seed

    logger.info("🌱 First run detected — seeding initial state (no notifications)...")
    current_results = fetch_results()
    if current_results:
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        _already_notified.update(all_results.keys())
        # Persist each baseline result's full data (candidates included) immediately,
        # so it's never lost even if ET later removes it from the website.
        for result in current_results:
            save_result(result)
        logger.info(f"✅ Saved {len(all_results)} existing results as baseline. "
                     "Only NEW results from now on will trigger notifications.")
    else:
        logger.warning("Could not fetch results for seeding. Will retry on next check.")


async def check_for_new_results(app: Application):
    """
    Scheduled job: scrape the results page, compare with saved state,
    and send notifications for any new entries.

    Guards against duplicate sends using both the persisted file state
    AND an in-memory set (_already_notified) that survives across cycles
    within the same session.
    """
    global _already_notified
    logger.info("🔍 Checking for new results...")

    current_results = fetch_results()
    if not current_results:
        logger.warning("No results fetched (page might be down). Skipping.")
        return

    last_results = load_last_results()

    # Persist every currently-live result's full data (candidates included) right away.
    # This is intentionally unconditional so candidate lists are captured instantly and
    # survive even if ET later edits or removes the announcement from the website.
    for result in current_results:
        save_result(result)

    # Safety: if no saved state exists yet, save and skip (don't spam)
    if not last_results:
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        _already_notified.update(all_results.keys())
        logger.info(f"📂 No prior state found. Saved {len(all_results)} results as baseline.")
        return

    # Find results that are new in BOTH the file state AND the in-memory guard.
    # The in-memory guard catches duplicates within the same session even if the
    # file hasn't been written yet (race condition protection).
    new_results = [
        r for r in current_results
        if r["id"] not in last_results and r["id"] not in _already_notified
    ]

    if new_results:
        logger.info(f"🆕 Found {len(new_results)} NEW result(s)!")

        # Mark as notified in-memory BEFORE sending so that if the bot restarts
        # mid-send, we don't re-send on the next cycle.
        for result in new_results:
            _already_notified.add(result["id"])

        # Persist the updated state immediately so a crash/restart won't re-send.
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        logger.info(f"✅ State saved. Total results tracked: {len(all_results)}")

        subscribers = load_subscribers()
        if not subscribers:
            logger.warning("No subscribers yet. New results won't be sent to anyone.")
        else:
            logger.info(f"📣 Sending to {len(subscribers)} subscriber(s)...")
            for result in new_results:
                logger.info(f"📤 Sending notification for: {result['position'][:50]}")
                for chat_id in list(subscribers):  # snapshot to avoid mutation during iteration
                    try:
                        await send_notification(app, chat_id, result)
                    except Exception as e:
                        logger.error(f"Unexpected error sending to {chat_id}: {e}")
                    await asyncio.sleep(0.05)  # Telegram rate limit: ~20 msg/s per bot
    else:
        logger.info("No new results found.")
        # Still update the state in case results were removed from the page
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)


# ─── Health Check Server ───────────────────────────────────────────────────

async def handle_health_check(request):
    """Simple health check endpoint for hosting providers."""
    return web.Response(text="Bot is running! ✈️")


async def handle_get_result_api(request):
    """API endpoint to return JSON data for a specific result ID."""
    result_id = request.match_info['id']
    data_path = os.path.join(DATA_DIR, "announcements", f"{result_id}.json")
    
    if not os.path.exists(data_path):
        return web.json_response({"error": "Result not found"}, status=404)
        
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return web.json_response(data)


async def handle_serve_viewer(request):
    """Serve the index.html viewer for any /results/{id} route."""
    viewer_path = os.path.join(os.getcwd(), "web", "index.html")
    if not os.path.exists(viewer_path):
        return web.Response(text="Viewer template not found", status=500)
    return web.FileResponse(viewer_path)


async def start_web_server():
    """Start a web server to keep the bot alive and serve result pages."""
    app = web.Application()
    
    # ─── Routes ──────────────────────────────────────────────────────────
    app.router.add_get("/", handle_health_check)
    
    # API for the template to fetch candidate data
    app.router.add_get("/api/results/{id}", handle_get_result_api)
    
    # The viewer page (served for all result IDs)
    app.router.add_get("/results/{id}", handle_serve_viewer)
    
    # Static files (CSS, JS)
    static_path = os.path.join(os.getcwd(), "web")
    app.router.add_static("/static/", static_path)
    
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

    # On platforms like Render, a previous instance may still be holding the
    # polling connection when a new deploy starts. Drop any active webhook and
    # wait a few seconds to let the old instance release the getUpdates lock
    # before we start polling, avoiding the 409 Conflict error.
    logger.info("🔄 Clearing any existing webhook / active polling session...")
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"delete_webhook failed (non-fatal): {e}")
    await asyncio.sleep(5)  # Give the old instance time to shut down

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

    # Seed initial state (saves baseline without sending notifications)
    seed_initial_state()

    logger.info(f"⏰ Checking every {CHECK_INTERVAL_SECONDS} seconds. Press Ctrl+C to stop.")

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
