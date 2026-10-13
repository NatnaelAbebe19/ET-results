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
from datetime import datetime
from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from config import (
    CHECK_INTERVAL_SECONDS,
    DATA_DIR,
    TELEGRAM_BOT_TOKEN,
    QUIET_HOURS_ENABLED,
    QUIET_HOURS_START,
    QUIET_HOURS_END,
    BOT_TIMEZONE,
)
from scraper import fetch_results
from bot import (
    build_application,
    load_subscribers,
    send_notification,
    matches_role_filter,
    is_pilot_result,
    is_cabin_crew_result,
    is_amt_result,
)
from result_parser import save_result
from database import (
    init_db,
    migrate_local_data,
    get_last_results,
    save_last_results as db_save_last_results,
    get_announcement,
    get_subscribers_with_filters,
    save_announcements_batch,
)

# ─── Base Directory ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── Logging Setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("et_results_bot")

# Silence noisy HTTP client and access polling logs on free tier hosting
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

# ─── In-Memory Sent Guard & Quiet Hours Tracking ──────────────────────────────
# Tracks result IDs already sent this session to prevent duplicate notifications
# even if the file state hasn't been flushed yet.
_already_notified: set[str] = set()
_in_quiet_mode: bool = False


def is_quiet_hours() -> bool:
    """
    Check if the current time is within quiet hours (default: 6 PM to 7 AM East Africa Time).
    During quiet hours, automated scraping and DB checks are paused to save resources.
    """
    global _in_quiet_mode
    if not QUIET_HOURS_ENABLED:
        return False

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(BOT_TIMEZONE)
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=3))

    now = datetime.now(tz)
    current_hour = now.hour

    if QUIET_HOURS_START > QUIET_HOURS_END:
        # Crosses midnight (e.g. 18 to 7: 18:00–23:59 and 00:00–06:59)
        quiet = current_hour >= QUIET_HOURS_START or current_hour < QUIET_HOURS_END
    else:
        quiet = QUIET_HOURS_START <= current_hour < QUIET_HOURS_END

    if quiet and not _in_quiet_mode:
        _in_quiet_mode = True
        logger.info(
            f"🌙 Entering quiet hours ({QUIET_HOURS_START}:00 - {QUIET_HOURS_END}:00 {BOT_TIMEZONE}). "
            "Automated checks paused until morning."
        )
    elif not quiet and _in_quiet_mode:
        _in_quiet_mode = False
        logger.info(
            f"☀️ Exiting quiet hours ({now.strftime('%H:%M')} {BOT_TIMEZONE}). "
            "Automated checks resumed!"
        )

    return quiet


# ─── State Management ───────────────────────────────────────────────────────

def load_last_results() -> dict[str, dict]:
    """Load previously seen results from database (or disk fallback)."""
    return get_last_results()


def save_last_results(results: dict[str, dict]):
    """Save current results to database and disk."""
    db_save_last_results(results)


# ─── Periodic Check Job ─────────────────────────────────────────────────────

async def seed_initial_state():
    """
    On startup, load saved state into the in-memory sent guard so existing results
    are never re-sent as notifications. Also fetch current results to batch-save
    any missing announcements (with candidate lists) into Neon Postgres in 1 batch,
    ensuring 'View Candidates' works even after redeploying.
    """
    global _already_notified
    last_results = load_last_results()
    if last_results:
        logger.info(f"📂 Found existing state with {len(last_results)} tracked results.")
        # Pre-populate the in-memory guard from saved state
        _already_notified.update(last_results.keys())

    # Pre-warm/sync candidate roster for current announcements
    logger.info("🌱 Ensuring all current announcements and candidate rosters are synchronized...")
    current_results = await fetch_results()
    if current_results:
        all_results = {r["id"]: r for r in current_results}
        if not last_results:
            save_last_results(all_results)
            _already_notified.update(all_results.keys())
            logger.info(f"✅ Saved {len(all_results)} existing results as baseline.")

        # Batch-save candidate lists in one fast DB query
        save_announcements_batch(current_results)
        logger.info(f"✅ Synchronized {len(current_results)} announcement details in database.")
    else:
        logger.warning("Could not fetch results during startup seeding. Will retry on next check.")


async def check_for_new_results(app: Application):
    """
    Scheduled job: scrape the results page, compare with saved state,
    and send notifications for any new entries.

    Guards against duplicate sends using both the persisted file state
    AND an in-memory set (_already_notified) that survives across cycles
    within the same session.
    """
    if is_quiet_hours():
        return

    global _already_notified
    logger.info("🔍 Checking for new results...")

    current_results = await fetch_results()
    if not current_results:
        logger.warning("No results fetched (page might be down). Skipping.")
        return

    last_results = load_last_results()

    # Safety: if no saved state exists yet, save and skip (don't spam)
    if not last_results:
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        _already_notified.update(all_results.keys())
        save_announcements_batch(current_results)
        logger.info(f"📂 No prior state found. Saved {len(all_results)} results as baseline.")
        return

    # Find results that are new in BOTH the file state AND the in-memory guard.
    new_results = [
        r for r in current_results
        if r["id"] not in last_results and r["id"] not in _already_notified
    ]

    if new_results:
        logger.info(f"🆕 Found {len(new_results)} NEW result(s)!")

        # Batch save full announcement data (with candidates) in a single DB query
        save_announcements_batch(new_results)

        # Mark as notified in-memory BEFORE sending so that if the bot restarts
        # mid-send, we don't re-send on the next cycle.
        for result in new_results:
            _already_notified.add(result["id"])

        # Persist the updated state immediately so a crash/restart won't re-send.
        all_results = {r["id"]: r for r in current_results}
        save_last_results(all_results)
        logger.info(f"✅ State saved. Total results tracked: {len(all_results)}")

        subscribers_map = get_subscribers_with_filters()
        if not subscribers_map:
            logger.warning("No subscribers yet. New results won't be sent to anyone.")
        else:
            logger.info(f"📣 Evaluating notifications for {len(subscribers_map)} subscriber(s)...")
            for result in new_results:
                pos_title = result.get("position", "Unknown")
                is_pilot = is_pilot_result(result)
                is_cabin = is_cabin_crew_result(result)
                is_amt = is_amt_result(result)
                is_aviation = is_pilot or is_cabin or is_amt

                def _matches(rf: str) -> bool:
                    if rf == "all":
                        return True
                    elif rf == "pilot":
                        return is_pilot
                    elif rf == "cabin":
                        return is_cabin
                    elif rf == "amt":
                        return is_amt
                    elif rf in ("aviation", "crew"):
                        return is_aviation
                    return True

                # Filter subscribers whose preference matches this announcement
                recipients = [
                    cid for cid, r_filter in subscribers_map.items()
                    if _matches(r_filter)
                ]

                logger.info(
                    f"📤 Sending notification for: {pos_title[:50]} "
                    f"to {len(recipients)}/{len(subscribers_map)} matched subscriber(s)..."
                )

                if not recipients:
                    continue

                # Send to matched subscribers concurrently (30 at a time to stay well
                # within Telegram's ~30 msg/s per bot rate limit)
                semaphore = asyncio.Semaphore(30)

                async def _send_one(chat_id: int, r: dict = result):
                    async with semaphore:
                        try:
                            await send_notification(app, chat_id, r)
                        except Exception as e:
                            logger.error(f"Unexpected error sending to {chat_id}: {e}")

                await asyncio.gather(*[_send_one(cid) for cid in recipients])
    else:
        logger.info("No new results found.")
        # Only update state if the set of tracked results actually changed
        # (e.g. a result was removed from or added back to the page)
        current_ids = {r["id"] for r in current_results}
        if current_ids != set(last_results.keys()):
            all_results = {r["id"]: r for r in current_results}
            save_last_results(all_results)
            logger.info(f"📂 Result set changed. Updated tracked state ({len(all_results)} results).")


# ─── Health Check Server ───────────────────────────────────────────────────

async def handle_health_check(request):
    """Simple health check endpoint for hosting providers."""
    return web.Response(text="Bot is running! ✈️")


async def handle_get_result_api(request):
    """API endpoint to return JSON data for a specific result ID."""
    result_id = request.match_info['id']
    data = get_announcement(result_id)
    if data:
        return web.json_response(data)

    data_path = os.path.join(DATA_DIR, "announcements", f"{result_id}.json")
    if not os.path.exists(data_path):
        return web.json_response({"error": "Result not found"}, status=404)
        
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return web.json_response(data)


async def handle_serve_viewer(request):
    """Serve the index.html viewer for any /results/{id} route."""
    viewer_path = os.path.join(BASE_DIR, "web", "index.html")
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
    static_path = os.path.join(BASE_DIR, "web")
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
    if QUIET_HOURS_ENABLED:
        logger.info(f"  Quiet hours: {QUIET_HOURS_START}:00 to {QUIET_HOURS_END}:00 ({BOT_TIMEZONE})")
    logger.info("=" * 60)

    # Initialize database tables and migrate any existing local files
    init_db()
    migrate_local_data()

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
    await seed_initial_state()

    # ─── Keep-Alive Ping ─────────────────────────────────────────────────────
    # Render's free tier sleeps after 15 minutes of inactivity, causing a
    # 30-50 second cold-start delay. Pinging our own health endpoint every
    # 14 minutes keeps the service warm and responsive at all times.
    async def _self_ping():
        try:
            from config import BASE_URL
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(BASE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logger.debug(f"🏓 Keep-alive ping → {resp.status}")
        except Exception as e:
            logger.debug(f"Keep-alive ping failed (non-fatal): {e}")

    scheduler.add_job(
        _self_ping,
        "interval",
        minutes=14,
        id="keep_alive",
        name="Keep-Alive Self-Ping",
        max_instances=1,
    )

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
