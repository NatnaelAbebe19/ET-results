"""
Discord Webhook Notifier for Ethiopian Airlines Results Checker.

Sends rich embed notifications to a Discord channel via webhook alongside
the Telegram bot, using aiohttp (no extra dependencies required).
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from config import (
    BASE_URL,
    DISCORD_ROLE_FILTER,
    DISCORD_WEBHOOK_URL,
    ET_RESULTS_URL,
)

logger = logging.getLogger(__name__)

# Ethiopian Airlines brand green for Discord embed accent bar
ET_GREEN_COLOR = 0x008A3D
MAX_FIELD_LENGTH = 1024


def is_discord_configured() -> bool:
    """Return True when a Discord webhook URL is configured."""
    return bool(DISCORD_WEBHOOK_URL)


def _truncate(text: str, limit: int = MAX_FIELD_LENGTH) -> str:
    """Safely truncate text to stay within Discord embed field limits."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_discord_payload(result: dict) -> dict:
    """Build a Discord webhook JSON payload with a rich embed for a result."""
    position = _truncate(result.get("position") or "Unknown Position")
    announcement = _truncate(result.get("announcement") or "")
    location = _truncate(result.get("location") or "")
    date_time = _truncate(result.get("date_time") or "")
    result_id = (result.get("id") or "").strip()
    candidates = result.get("candidates") or []

    viewer_url = f"{BASE_URL}/results/{result_id}" if result_id else ET_RESULTS_URL

    fields = [
        {"name": "📋 Position", "value": position, "inline": False},
    ]

    if announcement:
        fields.append({"name": "📢 Type", "value": announcement, "inline": True})

    if candidates:
        fields.append({"name": "👥 Candidates", "value": str(len(candidates)), "inline": True})

    if location:
        fields.append({"name": "📍 Location", "value": location, "inline": False})

    if date_time:
        fields.append({"name": "📅 Date & Time", "value": date_time, "inline": False})

    links = [f"[Visit Official Website]({ET_RESULTS_URL})"]
    if result_id and BASE_URL.startswith(("http://", "https://")):
        links.insert(0, f"[View Candidates]({viewer_url})")

    fields.append({"name": "🔗 Links", "value": " • ".join(links), "inline": False})

    embed = {
        "title": "✈️ NEW RESULT ANNOUNCEMENT",
        "url": viewer_url if viewer_url.startswith(("http://", "https://")) else ET_RESULTS_URL,
        "color": ET_GREEN_COLOR,
        "fields": fields,
        "footer": {"text": "Ethiopian Airlines Results Checker"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "username": "ET Results Checker",
        "embeds": [embed],
    }


async def send_discord_notification(
    result: dict,
    session: aiohttp.ClientSession | None = None,
    webhook_url: str | None = None,
) -> bool:
    """
    Send a single result announcement to Discord via webhook.
    Returns True on success, False otherwise (never raises).
    """
    url = (webhook_url or DISCORD_WEBHOOK_URL or "").strip()
    if not url:
        return False

    payload = build_discord_payload(result)
    owns_session = session is None
    client = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    try:
        for attempt in range(2):
            async with client.post(url, json=payload) as resp:
                if resp.status in (200, 204):
                    return True

                if resp.status == 429 and attempt == 0:
                    retry_after = 2.0
                    try:
                        body = await resp.json()
                        retry_after = min(float(body.get("retry_after", 2.0)), 10.0)
                    except Exception:
                        pass
                    logger.warning(f"Discord webhook rate-limited (429). Retrying in {retry_after:.1f}s...")
                    await asyncio.sleep(retry_after)
                    continue

                error_text = await resp.text()
                logger.error(f"Discord webhook returned HTTP {resp.status}: {error_text[:200]}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")
        return False
    finally:
        if owns_session:
            await client.close()

    return False


async def send_discord_notifications(results: list[dict]) -> int:
    """
    Send notifications for a list of new results to the configured Discord webhook,
    respecting DISCORD_ROLE_FILTER. Returns the number of alerts sent.
    """
    if not is_discord_configured() or not results:
        return 0

    role_filter = (DISCORD_ROLE_FILTER or "all").lower()
    if role_filter != "all":
        from bot import matches_role_filter

        matched_results = [r for r in results if matches_role_filter(r, role_filter)]
    else:
        matched_results = list(results)

    if not matched_results:
        logger.info(f"💬 Discord filter '{role_filter}' matched 0/{len(results)} new result(s).")
        return 0

    sent_count = 0
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for idx, result in enumerate(matched_results):
            pos_title = result.get("position", "Unknown")[:50]
            ok = await send_discord_notification(result, session=session)
            if ok:
                sent_count += 1
                logger.info(f"💬 Sent Discord notification for: {pos_title}")
            # Gentle delay between messages when multiple results arrive together
            if idx < len(matched_results) - 1:
                await asyncio.sleep(0.5)

    return sent_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    if not is_discord_configured():
        print("[ERROR] DISCORD_WEBHOOK_URL is not set in your .env file.")
        print("        Add: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        raise SystemExit(1)

    sample_result = {
        "id": "test-preview",
        "position": "TEST — TRAINEE PILOT CANDIDATES",
        "announcement": "WRITTEN EXAM (TEST NOTIFICATION)",
        "location": "Ethiopian Aviation University",
        "date_time": "Test alert — your Discord webhook is working!",
        "candidates": [{"no": "1", "name": "Sample Candidate"}],
    }

    success = asyncio.run(send_discord_notification(sample_result))
    if success:
        print("[OK] Test notification sent to Discord! Check your Discord channel.")
    else:
        print("[ERROR] Failed to send test notification. Double-check your DISCORD_WEBHOOK_URL.")
        raise SystemExit(1)
