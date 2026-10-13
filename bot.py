"""
Telegram Bot for Ethiopian Airlines Results Checker.
Handles user commands and sends notifications for new results.
"""

import json
import logging
import os
import re

from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, DATA_DIR, BASE_URL
from scraper import fetch_results
from database import (
    get_subscribers,
    add_subscriber,
    remove_subscriber,
    get_subscriber_filter,
    set_subscriber_filter,
    get_subscribers_with_filters,
    save_announcement,
)

logger = logging.getLogger(__name__)


# ─── Subscriber Management ───────────────────────────────────────────────────

def load_subscribers() -> set[int]:
    """Load subscriber chat IDs from Neon database (with local file fallback)."""
    return get_subscribers()


def save_subscribers(subscribers: set[int]):
    """Save subscriber chat IDs to database and local file."""
    for chat_id in subscribers:
        add_subscriber(chat_id)


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


_MD_ESCAPE_REGEX = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")
AMT_WORD_REGEX = re.compile(r"\bamt\b")


def _escape_md(text: str) -> str:
    """Escape special Markdown V2 characters efficiently."""
    if not text:
        return ""
    return _MD_ESCAPE_REGEX.sub(r"\\\1", str(text))


# ─── Role / Position Matching Logic ──────────────────────────────────────────

def is_pilot_result(result: dict) -> bool:
    """
    Check if announcement is for Pilots.
    Matches titles like 'ET-SPONSORED TRAINEE PILOT CANDIDATES', 'TRAINEE PILOT CANDIDATES',
    'CADET PILOT', 'FIRST OFFICER', 'CAPTAIN', etc.
    """
    fields = [
        result.get("position", ""),
        result.get("announcement", ""),
        result.get("description", ""),
    ]
    full_text = " ".join(fields).lower()
    pilot_keywords = [
        "pilot",
        "first officer",
        "captain",
        "cadet",
        "cockpit",
    ]
    return any(k in full_text for k in pilot_keywords)


def is_cabin_crew_result(result: dict) -> bool:
    """
    Check if announcement is for Cabin Crew.
    Matches titles like 'ET-SPONSORED TRAINEE CABIN CREW APPLICANT’S', 'CABIN CREW TRAINEE',
    'FLIGHT ATTENDANT', 'AIR HOSTESS', etc.
    """
    fields = [
        result.get("position", ""),
        result.get("announcement", ""),
        result.get("description", ""),
    ]
    full_text = " ".join(fields).lower()
    cabin_keywords = [
        "cabin crew",
        "flight attendant",
        "air hostess",
        "cabin attendant",
        "steward",
    ]
    return any(k in full_text for k in cabin_keywords)


def is_amt_result(result: dict) -> bool:
    """
    Check if announcement is for Aircraft Maintenance Technicians (AMT).
    Matches positions like:
      - TRAINEE A/C MAINTENANCE TECHNICIAN
      - TRAINEE A/C STRUCTURE
      - TRAINEE A/C CABIN MAINTENANCE
      - TRAINEE A/C AIRFRAME TECHNICIAN
      - TRAINEE A/C AVIONICS
      - TRAINEE A/C POWERPLANT TECHNICIAN
      - TRAINEE AIRCRAFT MECHANIC HARAR CANDIDATES
      - DIFFERENT AMT POSITIONS
      - ASSISTANT A/C MACHINING TECHNICIAN
    """
    fields = [
        result.get("position", ""),
        result.get("announcement", ""),
        result.get("description", ""),
    ]
    full_text = " ".join(fields).lower()

    amt_keywords = [
        "amt",
        "a/c maintenance",
        "a/c structure",
        "a/c cabin maintenance",
        "a/c airframe",
        "a/c avionics",
        "a/c powerplant",
        "aircraft mechanic",
        "a/c machining",
        "aircraft maintenance",
        "aviation maintenance",
        "aircraft technician",
        "avionics",
        "powerplant technician",
        "airframe technician",
    ]
    if any(k in full_text for k in amt_keywords):
        return True

    if AMT_WORD_REGEX.search(full_text):
        return True

    return False


def matches_role_filter(result: dict, role_filter: str) -> bool:
    """
    Determine if a result announcement matches a subscriber's role preference.
    Supported filters:
      - 'all': receives all announcements (default)
      - 'pilot': receives only Pilot announcements
      - 'cabin': receives only Cabin Crew announcements
      - 'amt': receives only Aircraft Maintenance Technician announcements
      - 'aviation' / 'crew': receives Pilot, Cabin Crew, OR AMT announcements
    """
    role = (role_filter or "all").lower()
    if role == "all":
        return True

    is_pilot = is_pilot_result(result)
    is_cabin = is_cabin_crew_result(result)
    is_amt = is_amt_result(result)

    if role == "pilot":
        return is_pilot
    elif role == "cabin":
        return is_cabin
    elif role == "amt":
        return is_amt
    elif role in ("aviation", "crew"):
        return is_pilot or is_cabin or is_amt

    return True


# ─── Filter UI Helpers ───────────────────────────────────────────────────────

FILTER_OPTIONS = {
    "pilot": {"label": "✈️ Pilot Only", "desc": "Trainee Pilot, Cadet & Cockpit roles"},
    "cabin": {"label": "🧑‍✈️ Cabin Crew", "desc": "Trainee Cabin Crew & Flight Attendants"},
    "amt": {"label": "🔧 AMT Only", "desc": "A/C Maintenance, Structure, Avionics & Mechanics"},
    "aviation": {"label": "✈️🛠️ All Aviation Roles", "desc": "Pilot, Cabin Crew & AMT combined"},
    "all": {"label": "🔔 All Announcements", "desc": "Every result published (Default)"},
}


def get_filter_keyboard(current_filter: str = "all") -> InlineKeyboardMarkup:
    """Build interactive inline keyboard with checked indicator for current filter."""
    curr = (current_filter or "all").lower()
    if curr == "crew":
        curr = "aviation"

    def label(code, text):
        return f"✅ {text}" if curr == code else text

    keyboard = [
        [
            InlineKeyboardButton(label("pilot", "✈️ Pilot Only"), callback_data="filter:pilot"),
            InlineKeyboardButton(label("cabin", "🧑‍✈️ Cabin Crew"), callback_data="filter:cabin"),
        ],
        [
            InlineKeyboardButton(label("amt", "🔧 AMT Only"), callback_data="filter:amt"),
            InlineKeyboardButton(label("aviation", "✈️🛠️ All Aviation"), callback_data="filter:aviation"),
        ],
        [
            InlineKeyboardButton(label("all", "🔔 All Announcements"), callback_data="filter:all"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def format_filter_message(current_filter: str = "all") -> str:
    """Format the preferences menu message text."""
    curr = (current_filter or "all").lower()
    if curr == "crew":
        curr = "aviation"
    current_label = FILTER_OPTIONS.get(curr, FILTER_OPTIONS["all"])["label"]

    return (
        "🎯 *Notification Preferences*\n\n"
        f"📌 *Current setting:* *{_escape_md(current_label)}*\n\n"
        f"📌 *All Aviation includes: Pilot, AMT and Cabin Crew*\n"
        "Tap a button below to update your preference instantly:"
    )


# ─── Bot Command Handlers ───────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — subscribe to notifications with clean, concise role selection."""
    chat_id = update.effective_chat.id
    add_subscriber(chat_id)
    current_filter = get_subscriber_filter(chat_id)
    if current_filter == "crew":
        current_filter = "aviation"

    welcome = (
        "👋 *Welcome to the ET Results Checker Bot\\!*\n\n"
        "✅ You are subscribed to notifications\\!\n\n"
        "📌 *Available Commands:*\n"
        "/filter — Choose notification preference\n"
        "/only\\_pilot — Switch to Pilot only\n"
        "/only\\_cabin — Switch to Cabin Crew only\n"
        "/only\\_amt — Switch to AMT Technicians only\n"
        "/aviation — All Aviation roles \\(Pilot \\+ Cabin \\+ AMT\\)\n"
        "/all\\_results — Receive all announcements\n"
        "/stop — Unsubscribe from notifications\n"
        "/help — Show help message"
    )
    await update.message.reply_text(
        welcome,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard(current_filter),
    )
    logger.info(f"Subscriber /start: {chat_id} (filter: {current_filter})")


async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /filter, /preferences — show interactive filter buttons."""
    chat_id = update.effective_chat.id
    add_subscriber(chat_id)
    current_filter = get_subscriber_filter(chat_id)
    text = format_filter_message(current_filter)
    await update.message.reply_text(
        text,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard(current_filter),
    )


async def only_pilot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /only_pilot — quick shortcut for Pilot Only notifications."""
    chat_id = update.effective_chat.id
    set_subscriber_filter(chat_id, "pilot")
    msg = (
        "✈️ *Preference Set: Pilot Only*\n\n"
        "You will now only receive notifications for *ET\\-SPONSORED TRAINEE PILOT CANDIDATES* "
        "and flight crew announcements\\.\n\n"
        "Tap below or use /filter anytime to change:"
    )
    await update.message.reply_text(
        msg,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard("pilot"),
    )
    logger.info(f"Chat {chat_id} set filter to pilot via command")


async def only_cabin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /only_cabin — quick shortcut for Cabin Crew Only notifications."""
    chat_id = update.effective_chat.id
    set_subscriber_filter(chat_id, "cabin")
    msg = (
        "🧑‍✈️ *Preference Set: Cabin Crew Only*\n\n"
        "You will now only receive notifications for *ET\\-SPONSORED TRAINEE CABIN CREW* "
        "and flight attendant announcements\\.\n\n"
        "Tap below or use /filter anytime to change:"
    )
    await update.message.reply_text(
        msg,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard("cabin"),
    )
    logger.info(f"Chat {chat_id} set filter to cabin via command")


async def only_amt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /only_amt — quick shortcut for AMT Technicians notifications."""
    chat_id = update.effective_chat.id
    set_subscriber_filter(chat_id, "amt")
    msg = (
        "🔧 *Preference Set: AMT Technicians*\n\n"
        "You will now only receive notifications for *Aircraft Maintenance Technician* \\(AMT\\), "
        "A/C Structure, Avionics, Airframe, and Mechanics announcements\\.\n\n"
        "Tap below or use /filter anytime to change:"
    )
    await update.message.reply_text(
        msg,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard("amt"),
    )
    logger.info(f"Chat {chat_id} set filter to amt via command")


async def aviation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /aviation (or /crew) — shortcut for All Aviation Roles."""
    chat_id = update.effective_chat.id
    set_subscriber_filter(chat_id, "aviation")
    msg = (
        "✈️🛠️ *Preference Set: All Aviation Roles*\n\n"
        "You will receive notifications for all *Pilot*, *Cabin Crew*, and *AMT Technician* announcements\\.\n\n"
        "Tap below or use /filter anytime to change:"
    )
    await update.message.reply_text(
        msg,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard("aviation"),
    )
    logger.info(f"Chat {chat_id} set filter to aviation via command")


async def all_results_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /all_results — quick shortcut for All Announcements."""
    chat_id = update.effective_chat.id
    set_subscriber_filter(chat_id, "all")
    msg = (
        "🔔 *Preference Set: All Announcements*\n\n"
        "You will now receive notifications for *all* Ethiopian Airlines results posted\\.\n\n"
        "Tap below or use /filter anytime to change:"
    )
    await update.message.reply_text(
        msg,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard("all"),
    )
    logger.info(f"Chat {chat_id} set filter to all via command")


async def filter_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on filter inline keyboard buttons."""
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = update.effective_chat.id
    data = query.data

    if not data.startswith("filter:"):
        return

    choice = data.split(":", 1)[1].lower()
    if choice == "crew":
        choice = "aviation"
    if choice not in FILTER_OPTIONS:
        choice = "all"

    # Save preference
    set_subscriber_filter(chat_id, choice)
    choice_label = FILTER_OPTIONS[choice]["label"]

    # Pop toast alert on Telegram
    await query.answer(f"Preference set: {choice_label}!")

    # Update buttons/text in place with new active checkmark
    try:
        if query.message and "Notification Preferences" in (query.message.text or ""):
            await query.edit_message_text(
                format_filter_message(choice),
                parse_mode="MarkdownV2",
                reply_markup=get_filter_keyboard(choice),
            )
        elif query.message:
            await query.edit_message_reply_markup(reply_markup=get_filter_keyboard(choice))
    except Exception as e:
        logger.debug(f"Message already up to date: {e}")

    # Send an explicit confirmation message in chat
    try:
        if choice == "all":
            detail = "You will now receive notifications for *all* Ethiopian Airlines career results posted\\."
        elif choice == "aviation":
            detail = "You will now receive notifications for *Pilot*, *Cabin Crew*, and *AMT Technician* announcements\\."
        elif choice == "pilot":
            detail = "You will now only receive notifications for *Trainee Pilot* announcements\\."
        elif choice == "cabin":
            detail = "You will now only receive notifications for *Trainee Cabin Crew* announcements\\."
        elif choice == "amt":
            detail = "You will now only receive notifications for *AMT Technician* announcements\\."
        else:
            detail = f"You will now receive notifications for:\n👉 *{_escape_md(choice_label)}*"

        await query.message.reply_text(
            f"✅ *Position Changed Successfully\\!*\n\n"
            f"🎯 *Active Filter:* *{_escape_md(choice_label)}*\n\n"
            f"{detail}\n\n"
            f"You can switch positions anytime using /filter or the buttons above\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Failed to send confirmation reply message: {e}")

    logger.info(f"Chat {chat_id} clicked filter: {choice}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop — unsubscribe from notifications."""
    chat_id = update.effective_chat.id
    remove_subscriber(chat_id)

    await update.message.reply_text(
        "🛑 You have been unsubscribed\\. Send /start to resubscribe\\.",
        parse_mode="MarkdownV2",
    )
    logger.info(f"Unsubscribed: {chat_id}")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check — manually trigger a check for new results."""
    await update.message.reply_text("🔍 Checking for new results...")
    results = await fetch_results()

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
    results = await fetch_results()

    if not results:
        await update.message.reply_text("❌ No results available right now.")
        return

    # Ensure the latest result's candidates are persisted in database
    try:
        save_announcement(results[0])
    except Exception as e:
        logger.debug(f"Could not persist latest announcement: {e}")

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
    """Handle /help — show available commands with interactive buttons."""
    help_text = (
        "✈️ *ET Results Checker Bot — Commands Help*\n\n"
        "🎯 *Notification Filters:*\n"
        "/filter — Open interactive buttons to choose what alerts you receive\n"
        "/only\\_pilot — Switch alerts to Trainee Pilot results only\n"
        "/only\\_cabin — Switch alerts to Cabin Crew results only\n"
        "/only\\_amt — Switch alerts to AMT Technicians only\n"
        "/aviation — All Aviation roles \\(Pilot, Cabin Crew, AMT\\)\n"
        "/all\\_results — Receive alerts for all Ethiopian Airlines results\n\n"
        "🔍 *Results & Status:*\n"
        "/latest — View the most recent result announcement\n"
        "/check — Check corporate site now for recent results\n"
        "/stop — Unsubscribe from all alerts\n"
        "/start — Restart bot & view current preferences"
    )
    chat_id = update.effective_chat.id
    current_filter = get_subscriber_filter(chat_id)
    await update.message.reply_text(
        help_text,
        parse_mode="MarkdownV2",
        reply_markup=get_filter_keyboard(current_filter),
    )


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

    # Role filter commands & button callbacks
    app.add_handler(CommandHandler("filter", filter_command))
    app.add_handler(CommandHandler("preferences", filter_command))
    app.add_handler(CommandHandler("role", filter_command))
    app.add_handler(CommandHandler("only_pilot", only_pilot_command))
    app.add_handler(CommandHandler("only_cabin", only_cabin_command))
    app.add_handler(CommandHandler("only_amt", only_amt_command))
    app.add_handler(CommandHandler("aviation", aviation_command))
    app.add_handler(CommandHandler("crew", aviation_command))
    app.add_handler(CommandHandler("all_results", all_results_command))
    app.add_handler(CallbackQueryHandler(filter_callback_handler, pattern=r"^filter:"))

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
