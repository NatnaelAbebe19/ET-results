"""
Telegram Bot for Ethiopian Airlines Results Checker.
Handles user commands and sends notifications for new results.
"""

import json
import logging
import os

from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, SUBSCRIBERS_FILE, DATA_DIR, BASE_URL
from scraper import fetch_results

logger = logging.getLogger(__name__)


# ─── Subscriber Management ───────────────────────────────────────────────────

def _ensure_data_dir():
    """Ensure the data directory exists."""
    os.makedirs(DATA_DIR, exist_ok=True)


def load_subscribers() -> set[int]:
    """Load subscriber chat IDs from file."""
    _ensure_data_dir()
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def save_subscribers(subscribers: set[int]):
    """Save subscriber chat IDs to file."""
    _ensure_data_dir()
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subscribers), f)


# ─── Message Formatting ─────────────────────────────────────────────────────

def format_result_message(result: dict) -> str:
    """Format a single result entry as a Telegram message."""
    msg = "✈️ *NEW RESULT ANNOUNCEMENT* ✈️\n\n"
    msg += f"📋 *Position:*\n{_escape_md(result['position'])}\n\n"

    if result.get("announcement"):
        msg += f"📢 *Type:*\n{_escape_md(result['announcement'])}\n\n"

    return msg


def format_summary_message(results: list[dict]) -> str:
    """Format a summary of all current results."""
    if not results:
        return "❌ No results found on the page right now."

    msg = f"📊 *Ethiopian Airlines — Current Results*\n"
    msg += f"Found *{len(results)}* announcement(s):\n\n"

    for i, r in enumerate(results[:10], 1):
        position = r["position"][:60]
        announcement = r.get("announcement", "")[:40]
        msg += f"*{i}.* {_escape_md(position)}\n"
        if announcement:
            msg += f"    _{_escape_md(announcement)}_\n"
        msg += "\n"

    if len(results) > 10:
        msg += f"_...and {len(results) - 10} more_\n\n"

    msg += f"🔗 [View All Results](https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results)"
    return msg


def _escape_md(text: str) -> str:
    """Escape special Markdown V2 characters."""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


# ─── Bot Command Handlers ───────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — subscribe to notifications."""
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    subscribers.add(chat_id)
    save_subscribers(subscribers)

    welcome = (
        "👋 *Welcome to the ET Results Checker Bot\\!*\n\n"
        "I monitor the Ethiopian Airlines careers results page and "
        "notify you whenever new results are posted\\.\n\n"
        "📌 *Commands:*\n"
        "/check \\— Manually check for new results\n"
        "/latest \\— View current results summary\n"
        "/stop \\— Unsubscribe from notifications\n"
        "/help \\— Show this help message\n\n"
        "✅ You are now subscribed to notifications\\!"
    )
    await update.message.reply_text(welcome, parse_mode="MarkdownV2")
    logger.info(f"New subscriber: {chat_id}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop — unsubscribe from notifications."""
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    subscribers.discard(chat_id)
    save_subscribers(subscribers)

    await update.message.reply_text(
        "🛑 You have been unsubscribed\\. Send /start to resubscribe\\.",
        parse_mode="MarkdownV2",
    )
    logger.info(f"Unsubscribed: {chat_id}")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check — manually trigger a check for new results."""
    await update.message.reply_text("🔍 Checking for new results...")
    results = fetch_results()

    if results:
        msg = format_summary_message(results)
        try:
            await update.message.reply_text(msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
        except Exception:
            # Fallback to plain text if markdown fails
            await update.message.reply_text(
                f"Found {len(results)} results. Visit: "
                "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"
            )
    else:
        await update.message.reply_text("❌ Could not fetch results. Will try again later.")


async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /latest — show the most recent results."""
    results = fetch_results()

    if not results:
        await update.message.reply_text("❌ No results available right now.")
        return

    # Show details of the first (most recent) result
    try:
        msg = format_result_message(results[0])
        viewer_url = f"{BASE_URL}/results/{results[0]['id']}"
        keyboard = [
            [
                InlineKeyboardButton("View Candidates", web_app=WebAppInfo(url=viewer_url)),
                InlineKeyboardButton("Visit Website", url="https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            msg, 
            parse_mode="MarkdownV2", 
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to send /latest format: {e}")
        r = results[0]
        await update.message.reply_text(
            f"Latest Result:\n\nPosition: {r['position']}\nType: {r.get('announcement', 'N/A')}\n\n"
            f"View: https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help — show available commands."""
    await start_command(update, context)


# ─── Notification Sender ────────────────────────────────────────────────────

async def send_notification(app: Application, chat_id: int, result: dict):
    """Send a new result notification to a specific chat."""
    try:
        msg = format_result_message(result)
        viewer_url = f"{BASE_URL}/results/{result['id']}"
        keyboard = [
            [
                InlineKeyboardButton("View Candidates", web_app=WebAppInfo(url=viewer_url)),
                InlineKeyboardButton("Visit Website", url="https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await app.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to send notification to {chat_id}: {e}")
        # Try plain text fallback
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🆕 New Result!\n\n"
                    f"Position: {result['position']}\n"
                    f"Type: {result.get('announcement', 'N/A')}\n\n"
                    f"View: https://corporate.ethiopianairlines.com/AboutEthiopian/careers/results"
                ),
            )
        except Exception as e2:
            logger.error(f"Plain text fallback also failed for {chat_id}: {e2}")


def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("latest", latest_command))
    app.add_handler(CommandHandler("help", help_command))

    # Register a global error handler so transient API errors (Conflict, NetworkError)
    # are logged cleanly instead of printing full tracebacks on every retry.
    from telegram.ext import TypeHandler
    async def _error_handler(update, context):
        from telegram.error import Conflict, NetworkError, TimedOut
        err = context.error
        if isinstance(err, Conflict):
            logger.warning("⚠️  Conflict: another bot instance is still active. Will retry...")
        elif isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"⚠️  Transient network error (will retry): {err}")
        else:
            logger.error(f"Unhandled error: {err}", exc_info=err)

    app.add_error_handler(_error_handler)

    return app
