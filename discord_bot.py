"""
Interactive Discord Bot for Ethiopian Airlines Results Checker.

Mirrors the Telegram bot's exact commands, clickable role filter buttons,
and database-persisted preferences while running alongside Telegram.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

from config import BASE_URL, DISCORD_BOT_TOKEN, ET_RESULTS_URL
from scraper import fetch_results
from bot import (
    FILTER_OPTIONS,
    is_pilot_result,
    is_cabin_crew_result,
    is_amt_result,
)
from database import (
    add_discord_subscriber,
    get_discord_subscriber_filter,
    get_discord_subscribers_with_filters,
    remove_discord_subscriber,
    save_announcement,
    set_discord_subscriber_filter,
)

logger = logging.getLogger(__name__)

# Ethiopian Airlines brand green for Discord embeds
ET_GREEN_COLOR = 0x008A3D
MAX_FIELD_LENGTH = 1024


def is_discord_bot_configured() -> bool:
    """Return True when DISCORD_BOT_TOKEN is configured."""
    return bool(DISCORD_BOT_TOKEN)


def _truncate(text: str, limit: int = MAX_FIELD_LENGTH) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_filter(role_filter: str | None) -> str:
    curr = (role_filter or "all").lower()
    if curr == "crew":
        curr = "aviation"
    return curr if curr in FILTER_OPTIONS else "all"


def _get_target(interaction: discord.Interaction) -> tuple[int, str]:
    """
    Determine subscription target from an interaction:
    - In DMs: subscribe the user ID ('user') so DMs survive restarts reliably.
    - In a server channel: subscribe the channel ID ('channel').
    """
    if interaction.guild is None:
        return interaction.user.id, "user"
    return interaction.channel_id, "channel"


def _filter_confirmation_detail(choice: str) -> str:
    if choice == "all":
        return "You will now receive notifications for **all** Ethiopian Airlines career results posted."
    if choice == "aviation":
        return "You will now receive notifications for **Pilot**, **Cabin Crew**, and **AMT Technician** announcements."
    if choice == "pilot":
        return "You will now only receive notifications for **Trainee Pilot** and flight crew announcements."
    if choice == "cabin":
        return "You will now only receive notifications for **Trainee Cabin Crew** and flight attendant announcements."
    if choice == "amt":
        return (
            "You will now only receive notifications for **Aircraft Maintenance Technician (AMT)**, "
            "A/C Structure, Avionics, Airframe, and Mechanics announcements."
        )
    return f"You will now receive notifications for **{FILTER_OPTIONS.get(choice, FILTER_OPTIONS['all'])['label']}**."


# ─── Embeds & UI Components ──────────────────────────────────────────────────

def build_result_embed(result: dict) -> discord.Embed:
    """Build a rich Discord embed for a single result announcement."""
    position = _truncate(result.get("position") or "Unknown Position")
    announcement = _truncate(result.get("announcement") or "")
    location = _truncate(result.get("location") or "")
    date_time = _truncate(result.get("date_time") or "")
    result_id = (result.get("id") or "").strip()
    candidates = result.get("candidates") or []

    viewer_url = f"{BASE_URL}/results/{result_id}" if result_id else ET_RESULTS_URL
    embed_url = viewer_url if viewer_url.startswith(("http://", "https://")) else ET_RESULTS_URL

    embed = discord.Embed(
        title="✈️ NEW RESULT ANNOUNCEMENT",
        url=embed_url,
        color=ET_GREEN_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="📋 Position", value=position, inline=False)

    if announcement:
        embed.add_field(name="📢 Type", value=announcement, inline=True)

    if candidates:
        embed.add_field(name="👥 Candidates", value=str(len(candidates)), inline=True)

    if location:
        embed.add_field(name="📍 Location", value=location, inline=False)

    if date_time:
        embed.add_field(name="📅 Date & Time", value=date_time, inline=False)

    embed.set_footer(text="Ethiopian Airlines Results Checker")
    return embed


def build_result_links_view(result: dict) -> discord.ui.View:
    """Build clickable URL buttons matching Telegram's View Candidates & Visit Website buttons."""
    view = discord.ui.View()
    result_id = (result.get("id") or "").strip()

    if result_id and BASE_URL.startswith(("http://", "https://")):
        view.add_item(
            discord.ui.Button(
                label="View Candidates",
                style=discord.ButtonStyle.link,
                url=f"{BASE_URL}/results/{result_id}",
            )
        )

    view.add_item(
        discord.ui.Button(
            label="Visit Website",
            style=discord.ButtonStyle.link,
            url=ET_RESULTS_URL,
        )
    )
    return view


def build_summary_embed(results: list[dict]) -> discord.Embed:
    """Build a summary embed of current results (for /check)."""
    if not results:
        return discord.Embed(
            title="📊 Ethiopian Airlines — Current Results",
            description="❌ No results found on the page right now.",
            color=0xE74C3C,
        )

    lines = [f"Found **{len(results)}** announcement(s):\n"]
    for i, r in enumerate(results[:10], 1):
        position = (r.get("position") or "Unknown")[:65]
        announcement = (r.get("announcement") or "")[:45]
        line = f"**{i}.** {position}"
        if announcement:
            line += f"\n> _{announcement}_"
        lines.append(line)

    if len(results) > 10:
        lines.append(f"\n_...and {len(results) - 10} more_")

    lines.append(f"\n🔗 [View All Results]({ET_RESULTS_URL})")

    embed = discord.Embed(
        title="📊 Ethiopian Airlines — Current Results",
        description="\n".join(lines)[:4096],
        color=ET_GREEN_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Ethiopian Airlines Results Checker")
    return embed


def build_filter_embed(current_filter: str = "all") -> discord.Embed:
    """Build the Notification Preferences embed (matching Telegram's /filter)."""
    curr = _normalize_filter(current_filter)
    current_label = FILTER_OPTIONS[curr]["label"]

    embed = discord.Embed(
        title="🎯 Notification Preferences",
        description=(
            f"📌 **Current setting:** **{current_label}**\n\n"
            "📌 **All Aviation:** Pilot, AMT and Cabin Crew\n\n"
            "Click a button below to update your preference instantly:"
        ),
        color=ET_GREEN_COLOR,
    )
    embed.set_footer(text="Ethiopian Airlines Results Checker")
    return embed


class FilterButton(discord.ui.Button):
    """Persistent button for selecting a notification role filter."""

    def __init__(self, filter_code: str, base_label: str, is_active: bool, row: int):
        label = f"✅ {base_label}" if is_active else base_label
        style = discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary
        super().__init__(
            label=label,
            style=style,
            custom_id=f"et_filter:{filter_code}",
            row=row,
        )
        self.filter_code = filter_code

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        target_id, target_type = _get_target(interaction)
        choice = _normalize_filter(self.filter_code)
        set_discord_subscriber_filter(target_id, choice, target_type)
        choice_label = FILTER_OPTIONS[choice]["label"]

        # Update original message buttons/embed in place with the active checkmark
        try:
            new_view = FilterView(current_filter=choice)
            if interaction.message:
                has_prefs_title = bool(
                    interaction.message.embeds
                    and "Notification Preferences" in (interaction.message.embeds[0].title or "")
                )
                if has_prefs_title:
                    await interaction.message.edit(
                        embed=build_filter_embed(choice),
                        view=new_view,
                    )
                else:
                    await interaction.message.edit(view=new_view)
        except Exception as e:
            logger.debug(f"Could not update filter message in place: {e}")

        # Send explicit confirmation message (matching Telegram behavior)
        confirm_embed = discord.Embed(
            title="✅ Position Changed Successfully!",
            description=(
                f"🎯 **Active Filter:** **{choice_label}**\n\n"
                f"{_filter_confirmation_detail(choice)}\n\n"
                "You can switch positions anytime using `/filter` or the buttons above."
            ),
            color=ET_GREEN_COLOR,
        )
        await interaction.followup.send(embed=confirm_embed)
        logger.info(f"Discord {target_type} {target_id} clicked filter: {choice}")


class FilterView(discord.ui.View):
    """Persistent interactive filter buttons matching Telegram's inline keyboard."""

    def __init__(self, current_filter: str | None = None):
        super().__init__(timeout=None)
        curr = _normalize_filter(current_filter) if current_filter is not None else ""

        buttons = [
            ("pilot", "✈️ Pilot Only", 0),
            ("cabin", "🧑‍✈️ Cabin Crew", 0),
            ("amt", "🔧 AMT Only", 1),
            ("aviation", "✈️🛠️ All Aviation", 1),
            ("all", "🔔 All Announcements", 2),
        ]
        for code, text, row in buttons:
            self.add_item(FilterButton(code, text, is_active=(curr == code), row=row))


# ─── Discord Bot Client ──────────────────────────────────────────────────────

class ETResultsDiscordBot(discord.Client):
    """Lightweight Discord Client using Slash Commands + Persistent Views (no privileged intents)."""

    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def setup_hook(self):
        # Register persistent FilterView so buttons keep working across restarts
        self.add_view(FilterView())

        # Sync slash commands globally (works in servers and DMs with zero duplicates)
        try:
            synced = await self.tree.sync()
            logger.info(f"💬 Synced {len(synced)} Discord slash command(s) globally.")
        except Exception as e:
            logger.error(f"Failed to sync Discord slash commands: {e}")

    async def on_ready(self):
        logger.info(f"💬 Discord bot logged in as {self.user} (ID: {self.user.id})")

    def _register_commands(self):
        tree = self.tree

        @tree.command(name="start", description="Subscribe this channel (or DM) to ET Results notifications")
        async def start_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            target_id, target_type = _get_target(interaction)
            add_discord_subscriber(target_id, target_type=target_type)
            current_filter = _normalize_filter(get_discord_subscriber_filter(target_id))

            where_label = "this DM" if target_type == "user" else "this channel"
            embed = discord.Embed(
                title="👋 Welcome to the ET Results Checker Bot!",
                description=(
                    f"✅ Alerts are now active for **{where_label}**!\n"
                    f"🎯 **Current filter:** **{FILTER_OPTIONS[current_filter]['label']}**\n\n"
                    "📌 **Available Commands:**\n"
                    "`/filter` — Choose notification preference\n"
                    "`/only_pilot` — Switch to Pilot only\n"
                    "`/only_cabin` — Switch to Cabin Crew only\n"
                    "`/only_amt` — Switch to AMT Technicians only\n"
                    "`/aviation` — All Aviation roles (Pilot + Cabin + AMT)\n"
                    "`/all_results` — Receive all announcements\n"
                    "`/latest` — View the most recent result\n"
                    "`/check` — Check corporate site now\n"
                    "`/stop` — Unsubscribe from notifications\n"
                    "`/help` — Show help message"
                ),
                color=ET_GREEN_COLOR,
            )
            embed.set_footer(text="Click a button below to choose your filter")
            await interaction.followup.send(embed=embed, view=FilterView(current_filter))
            logger.info(f"Discord subscriber /start: {target_type} {target_id} (filter: {current_filter})")

        @tree.command(name="filter", description="Choose which ET result categories you receive")
        async def filter_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            target_id, target_type = _get_target(interaction)
            add_discord_subscriber(target_id, target_type=target_type)
            current_filter = _normalize_filter(get_discord_subscriber_filter(target_id))
            await interaction.followup.send(
                embed=build_filter_embed(current_filter),
                view=FilterView(current_filter),
            )

        async def _apply_shortcut(interaction: discord.Interaction, choice: str, title: str):
            await interaction.response.defer()
            target_id, target_type = _get_target(interaction)
            set_discord_subscriber_filter(target_id, choice, target_type)
            embed = discord.Embed(
                title=title,
                description=(
                    f"{_filter_confirmation_detail(choice)}\n\n"
                    "Click a button below or use `/filter` anytime to change:"
                ),
                color=ET_GREEN_COLOR,
            )
            await interaction.followup.send(embed=embed, view=FilterView(choice))
            logger.info(f"Discord {target_type} {target_id} set filter to {choice} via command")

        @tree.command(name="only_pilot", description="Receive only Trainee Pilot & flight crew announcements")
        async def only_pilot_cmd(interaction: discord.Interaction):
            await _apply_shortcut(interaction, "pilot", "✈️ Preference Set: Pilot Only")

        @tree.command(name="only_cabin", description="Receive only Trainee Cabin Crew announcements")
        async def only_cabin_cmd(interaction: discord.Interaction):
            await _apply_shortcut(interaction, "cabin", "🧑‍✈️ Preference Set: Cabin Crew Only")

        @tree.command(name="only_amt", description="Receive only AMT Technician announcements")
        async def only_amt_cmd(interaction: discord.Interaction):
            await _apply_shortcut(interaction, "amt", "🔧 Preference Set: AMT Technicians")

        @tree.command(name="aviation", description="Receive all Aviation roles (Pilot + Cabin Crew + AMT)")
        async def aviation_cmd(interaction: discord.Interaction):
            await _apply_shortcut(interaction, "aviation", "✈️🛠️ Preference Set: All Aviation Roles")

        @tree.command(name="all_results", description="Receive all Ethiopian Airlines result announcements")
        async def all_results_cmd(interaction: discord.Interaction):
            await _apply_shortcut(interaction, "all", "🔔 Preference Set: All Announcements")

        @tree.command(name="stop", description="Unsubscribe this channel (or DM) from notifications")
        async def stop_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            target_id, target_type = _get_target(interaction)
            remove_discord_subscriber(target_id)
            embed = discord.Embed(
                title="🛑 Unsubscribed",
                description="You have been unsubscribed from ET Results alerts. Run `/start` anytime to resubscribe.",
                color=0x95A5A6,
            )
            await interaction.followup.send(embed=embed)
            logger.info(f"Discord unsubscribed: {target_type} {target_id}")

        @tree.command(name="check", description="Check the ET corporate site now for current results")
        async def check_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            results = await fetch_results()
            if results:
                await interaction.followup.send(embed=build_summary_embed(results))
            else:
                await interaction.followup.send("❌ Could not fetch results right now. Will try again later.")

        @tree.command(name="latest", description="View the most recent ET result announcement")
        async def latest_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            results = await fetch_results()
            if not results:
                await interaction.followup.send("❌ No results available right now.")
                return

            latest = results[0]
            try:
                save_announcement(latest)
            except Exception as e:
                logger.debug(f"Could not persist latest announcement: {e}")

            await interaction.followup.send(
                embed=build_result_embed(latest),
                view=build_result_links_view(latest),
            )

        @tree.command(name="help", description="Show available commands and filter options")
        async def help_cmd(interaction: discord.Interaction):
            await interaction.response.defer()
            target_id, _ = _get_target(interaction)
            current_filter = _normalize_filter(get_discord_subscriber_filter(target_id))

            embed = discord.Embed(
                title="✈️ ET Results Checker Bot — Commands Help",
                description=(
                    "🎯 **Notification Filters:**\n"
                    "`/filter` — Open interactive buttons to choose what alerts you receive\n"
                    "`/only_pilot` — Switch alerts to Trainee Pilot results only\n"
                    "`/only_cabin` — Switch alerts to Cabin Crew results only\n"
                    "`/only_amt` — Switch alerts to AMT Technicians only\n"
                    "`/aviation` — All Aviation roles (Pilot, Cabin Crew, AMT)\n"
                    "`/all_results` — Receive alerts for all Ethiopian Airlines results\n\n"
                    "🔍 **Results & Status:**\n"
                    "`/latest` — View the most recent result announcement\n"
                    "`/check` — Check corporate site now for recent results\n"
                    "`/stop` — Unsubscribe from all alerts\n"
                    "`/start` — Subscribe & view current preferences"
                ),
                color=ET_GREEN_COLOR,
            )
            await interaction.followup.send(embed=embed, view=FilterView(current_filter))


def build_discord_bot() -> ETResultsDiscordBot:
    """Create and return the configured Discord bot instance."""
    return ETResultsDiscordBot()


# ─── Notification Sender ─────────────────────────────────────────────────────

async def _resolve_destination(bot: ETResultsDiscordBot, target_id: int, target_type: str):
    """Resolve a Discord Messageable (TextChannel/Thread or User DM)."""
    if target_type == "user":
        user = bot.get_user(target_id)
        if user is None:
            user = await bot.fetch_user(target_id)
        return user

    channel = bot.get_channel(target_id)
    if channel is None:
        channel = await bot.fetch_channel(target_id)
    return channel


async def send_discord_bot_notifications(bot: ETResultsDiscordBot, new_results: list[dict]) -> int:
    """
    Send notifications for new results to all subscribed Discord channels/users
    whose role filter matches each announcement (same matching logic as Telegram).
    """
    if not bot or not new_results:
        return 0

    if not bot.is_ready():
        logger.warning("Discord bot is not ready yet; skipping Discord bot notifications for this cycle.")
        return 0

    subscribers_map = get_discord_subscribers_with_filters()
    if not subscribers_map:
        logger.info("💬 No Discord bot subscribers yet. Run /start in Discord to subscribe.")
        return 0

    logger.info(f"💬 Evaluating Discord notifications for {len(subscribers_map)} subscriber(s)...")
    sent_count = 0

    for result in new_results:
        pos_title = result.get("position", "Unknown")
        is_pilot = is_pilot_result(result)
        is_cabin = is_cabin_crew_result(result)
        is_amt = is_amt_result(result)
        is_aviation = is_pilot or is_cabin or is_amt

        def _matches(rf: str) -> bool:
            rf = (rf or "all").lower()
            if rf == "all":
                return True
            if rf == "pilot":
                return is_pilot
            if rf == "cabin":
                return is_cabin
            if rf == "amt":
                return is_amt
            if rf in ("aviation", "crew"):
                return is_aviation
            return True

        recipients = [
            (tid, info["target_type"])
            for tid, info in subscribers_map.items()
            if _matches(info.get("role_filter", "all"))
        ]

        logger.info(
            f"💬 Sending Discord notification for: {pos_title[:50]} "
            f"to {len(recipients)}/{len(subscribers_map)} matched subscriber(s)..."
        )

        if not recipients:
            continue

        embed = build_result_embed(result)
        semaphore = asyncio.Semaphore(5)

        async def _send_one(tid: int, t_type: str):
            nonlocal sent_count
            async with semaphore:
                try:
                    dest = await _resolve_destination(bot, tid, t_type)
                    if dest is None:
                        logger.warning(f"Could not resolve Discord {t_type} {tid}.")
                        return
                    await dest.send(embed=embed, view=build_result_links_view(result))
                    sent_count += 1
                except discord.Forbidden:
                    logger.warning(
                        f"Missing permission to message Discord {t_type} {tid} "
                        "(DMs closed or channel permissions missing)."
                    )
                except discord.NotFound:
                    logger.warning(f"Discord {t_type} {tid} no longer exists.")
                except Exception as e:
                    logger.error(f"Unexpected error sending Discord alert to {t_type} {tid}: {e}")

        await asyncio.gather(*[_send_one(tid, t_type) for tid, t_type in recipients])

    return sent_count
