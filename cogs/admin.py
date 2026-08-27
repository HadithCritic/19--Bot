"""RSS feed monitoring, the daily reminder, and general utility commands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import time as dt_time
from pathlib import Path

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

from core.checks import staff_only
from core.config import CONFIG, EST
from core.errors import respond

logger = logging.getLogger(__name__)

_FEED_TIMEOUT = aiohttp.ClientTimeout(total=30)
_FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SubmissionServerBot/2.0)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9",
}
# Guard against announcing an entire backlog if a feed rewrites its permalinks.
_MAX_ANNOUNCEMENTS_PER_CHECK = 3
_PAUSE_BETWEEN_FEEDS_SECONDS = 2

# Per-feed backoff after HTTP 429. Reddit rate limits .rss aggressively and
# reports x-ratelimit-remaining: 0 even on a successful response, so hammering
# it every cycle just earns more 429s.
_DEFAULT_BACKOFF_SECONDS = 900.0
_MAX_BACKOFF_SECONDS = 3600.0

_RULES_WINDOW_SECONDS = 300
_RULES_MAX_USES = 3
_RULES_TRACKED_USERS_CAP = 500


@dataclass(frozen=True, slots=True)
class Feed:
    """One monitored RSS or Atom source."""

    feed_id: str
    name: str
    url: str
    rss_url: str
    emoji: str
    channel_id: int
    ping_role_id: int | None = None


def _build_feeds() -> tuple[Feed, ...]:
    return (
        Feed(
            feed_id="qurantalkblog",
            name="Quran Talk Blog",
            url="https://qurantalkblog.com/",
            rss_url="https://qurantalkblog.com/feed/",
            emoji="📜",
            channel_id=CONFIG.content_channel_id,
        ),
        Feed(
            # The site moved off WordPress, so /feed/ now 404s and this feed had
            # been silently failing. Verified 2026-08-27: /rss.xml serves 81 entries.
            feed_id="hadithcriticblog",
            name="Hadith Critic Blog",
            url="https://hadithcriticblog.com/",
            rss_url="https://hadithcriticblog.com/rss.xml",
            emoji="⚖️",
            channel_id=CONFIG.content_channel_id,
        ),
        Feed(
            feed_id="submissiontogod",
            name="Submission to God",
            url="https://submissiontogod.wordpress.com/",
            rss_url="https://submissiontogod.wordpress.com/feed/",
            emoji="🤲",
            channel_id=CONFIG.content_channel_id,
        ),
        Feed(
            feed_id="dutchkickboxing_medium",
            name="EJ's Medium Blog",
            url="https://medium.com/@dutchkickboxing1on1",
            rss_url="https://medium.com/feed/@dutchkickboxing1on1",
            emoji="✍️",
            channel_id=CONFIG.content_channel_id,
        ),
        Feed(
            feed_id="hadithcriticism_reddit",
            name="Hadith Criticism Reddit",
            url="https://www.reddit.com/r/HadithCriticism/",
            rss_url="https://www.reddit.com/r/HadithCriticism/.rss",
            emoji="🔴",
            channel_id=CONFIG.reddit_channel_id,
            ping_role_id=CONFIG.reddit_notification_role_id,
        ),
    )


def select_new_entries(entries: list[dict], last_entry_id: str | None) -> list[dict]:
    """Return unseen entries oldest-first.

    The old implementation only ever looked at entries[0], so a second post
    inside the same five-minute window was never announced.
    """
    if not entries:
        return []
    if last_entry_id is None:
        return []

    unseen: list[dict] = []
    for entry in entries:
        if entry.get("link") == last_entry_id:
            break
        unseen.append(entry)
    else:
        # The stored id is no longer in the feed window; only announce the newest
        # rather than replaying an unbounded backlog.
        unseen = entries[:1]

    unseen.reverse()
    return unseen[-_MAX_ANNOUNCEMENTS_PER_CHECK:]


class Admin(commands.Cog):
    """Content automation and utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]
        self.feeds = _build_feeds()
        self._session: aiohttp.ClientSession | None = None
        self._rules_usage: dict[int, list[float]] = {}
        # feed_id -> monotonic time before which the feed must not be polled
        self._feed_backoff: dict[str, float] = {}

    async def cog_load(self) -> None:
        # One session for the cog's lifetime instead of one per five-minute cycle.
        self._session = aiohttp.ClientSession(timeout=_FEED_TIMEOUT, headers=_FEED_HEADERS)
        await self._import_legacy_blog_state()
        self.check_feeds.start()
        self.daily_reminder.start()

    async def cog_unload(self) -> None:
        self.check_feeds.cancel()
        self.daily_reminder.cancel()
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- Commands ---

    @app_commands.command(name="rules", description="Display the server rules link.")
    @app_commands.guild_only()
    async def rules(self, interaction: discord.Interaction) -> None:
        if not self._allow_rules_use(interaction.user.id):
            await respond(interaction, "Please wait before using this command again.")
            return
        await respond(
            interaction,
            f"📜 **Server Rules:** {CONFIG.rules_channel_url}",
            ephemeral=False,
        )

    @app_commands.command(
        name="privacy",
        description="What data this bot stores, and how to have it deleted.",
    )
    async def privacy(self, interaction: discord.Interaction) -> None:
        """Surface the policies in-client.

        Discord requires these links in the Developer Portal for verification;
        the Developer Terms of Service additionally require that the privacy
        policy be easily accessible to users from the application itself.
        """
        embed = discord.Embed(
            title="🔒 Data and Privacy",
            description=(
                "This bot stores only what it needs to moderate the server: "
                "moderation records, role expiry timers, and staff-created events. "
                "It reads message text to block links from new members, but does "
                "not save that text. It never sells or shares your data."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Full policies",
            value=(
                f"• [Privacy Policy]({CONFIG.privacy_policy_url})\n"
                f"• [Terms of Service]({CONFIG.terms_of_service_url})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Your data",
            value=(
                "You can request a copy of your data, or its deletion, at any time. "
                "Contact a server administrator or the address in the privacy policy."
            ),
            inline=False,
        )
        embed.set_footer(text="Operates under Discord's Terms of Service and Developer Policy")
        await respond(interaction, embed=embed)

    @app_commands.command(
        name="commands19", description="Show the full list of bot commands (moderators only)."
    )
    @app_commands.guild_only()
    @staff_only()
    async def commands19(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="19 Bot Commands",
            description="Overview of all available commands and how to use them.",
            color=discord.Color.dark_theme(),
        )
        embed.add_field(
            name="Moderation",
            value=(
                "`/ptimeout <user> [reason]` — apply a 28-day auto-renewing timeout.\n"
                "`/undoptimeout <user>` — remove a perpetual timeout.\n"
                "`/listptimeout` — list all active perpetual timeouts."
            ),
            inline=False,
        )
        embed.add_field(
            name="Security",
            value=(
                "`/botwhitelist add <id>` — allow a bot to join.\n"
                "`/botwhitelist remove <id>` — revoke a bot's access.\n"
                "`/botwhitelist list` — show every allowed bot."
            ),
            inline=False,
        )
        embed.add_field(
            name="Live Events",
            value=(
                "`/addevent` — open a form to schedule a live event.\n"
                "`/listevents` — show all active live events.\n"
                "`/deleteevent <id>` — deactivate an event by its ID."
            ),
            inline=False,
        )
        embed.add_field(
            name="Utilities",
            value=(
                "`/debate` — post a reference diagram into the channel.\n"
                "`/rules` — post the server rules link.\n"
                "`/privacy` — what data the bot stores and how to have it removed."
            ),
            inline=False,
        )
        await respond(interaction, embed=embed)

    # --- Tasks ---

    @tasks.loop(minutes=5)
    async def check_feeds(self) -> None:
        now = time.monotonic()
        for feed in self.feeds:
            ready_at = self._feed_backoff.get(feed.feed_id, 0.0)
            if now < ready_at:
                logger.debug(
                    "Skipping feed %s for another %.0fs (backing off)",
                    feed.feed_id,
                    ready_at - now,
                )
                continue
            try:
                await self._check_feed(feed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error checking feed %s", feed.feed_id)
            await asyncio.sleep(_PAUSE_BETWEEN_FEEDS_SECONDS)

    @tasks.loop(time=dt_time(19, 19, tzinfo=EST))
    async def daily_reminder(self) -> None:
        """Remind staff in mod-chat to reinforce the server Iron Dome."""
        guild = self.bot.get_guild(CONFIG.guild_id)
        if guild is None:
            logger.warning("Guild unavailable, skipping the daily reminder")
            return

        channel = guild.get_channel(CONFIG.mod_chat_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logger.error("Mod chat channel %s unavailable", CONFIG.mod_chat_channel_id)
            return

        try:
            await channel.send(
                f"<@{CONFIG.hadithcritic_user_id}> This is a reminder to reinforce "
                "the server Iron Dome.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            logger.info("Sent the daily Iron Dome reminder")
        except discord.HTTPException as exc:
            logger.error("Failed to send the daily reminder: %s", exc)

    @check_feeds.before_loop
    @daily_reminder.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    @check_feeds.error
    @daily_reminder.error
    async def _task_error(self, error: BaseException) -> None:
        logger.exception("Admin background task failed", exc_info=error)

    # --- Feed helpers ---

    async def _check_feed(self, feed: Feed) -> None:
        if self._session is None:
            return

        try:
            async with self._session.get(feed.rss_url) as response:
                if response.status == 429:
                    # Expected from Reddit, not a misconfiguration.
                    delay = self._note_rate_limit(feed, response.headers.get("Retry-After"))
                    logger.info("Feed %s rate limited, backing off %.0fs", feed.feed_id, delay)
                    return
                if response.status != 200:
                    logger.warning(
                        "Feed %s returned HTTP %s for %s. If this persists the feed "
                        "URL has probably moved; run scripts/check_feeds.py",
                        feed.feed_id,
                        response.status,
                        feed.rss_url,
                    )
                    return
                self._feed_backoff.pop(feed.feed_id, None)
                body = await response.text()
        except TimeoutError:
            logger.warning("Timeout fetching feed %s", feed.feed_id)
            return
        except aiohttp.ClientError as exc:
            # Transient network problems are normal on a long-running bot.
            logger.debug("Network error fetching feed %s: %s", feed.feed_id, exc)
            return

        # feedparser is synchronous and CPU-bound on large feeds.
        parsed = await asyncio.to_thread(feedparser.parse, body)
        entries = [dict(entry) for entry in parsed.entries]
        if not entries:
            logger.debug("Feed %s produced no entries", feed.feed_id)
            return

        newest_id = entries[0].get("link")
        if not newest_id:
            return

        stored = await self._get_feed_state(feed.feed_id)
        if stored is None:
            # First sight of this feed: record a baseline, announce nothing.
            await self._set_feed_state(feed.feed_id, newest_id)
            logger.info("Baselined feed %s at %s", feed.feed_id, newest_id)
            return

        for entry in select_new_entries(entries, stored):
            await self._announce(feed, entry)

        if newest_id != stored:
            await self._set_feed_state(feed.feed_id, newest_id)

    def _note_rate_limit(self, feed: Feed, retry_after: str | None) -> float:
        """Record a backoff for one feed, honouring Retry-After when present."""
        delay = _DEFAULT_BACKOFF_SECONDS
        if retry_after:
            # Some servers send an HTTP-date instead of seconds; the default covers it.
            with contextlib.suppress(ValueError):
                delay = max(float(retry_after), 1.0)
        # Double on repeat offences, so a persistently limited feed goes quiet.
        previous = self._feed_backoff.get(feed.feed_id)
        if previous is not None and previous > time.monotonic():
            delay *= 2
        delay = min(delay, _MAX_BACKOFF_SECONDS)
        self._feed_backoff[feed.feed_id] = time.monotonic() + delay
        return delay

    async def _announce(self, feed: Feed, entry: dict) -> None:
        channel = self.bot.get_channel(feed.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logger.error("Feed channel %s unavailable for %s", feed.channel_id, feed.feed_id)
            return

        title = entry.get("title") or "Untitled"
        link = entry.get("link") or feed.url
        message = f"{feed.emoji} **New Post: {title}**\n{link}"
        mentions = discord.AllowedMentions.none()
        if feed.ping_role_id is not None:
            message = f"<@&{feed.ping_role_id}> {message}"
            mentions = discord.AllowedMentions(roles=True)

        try:
            await channel.send(message, allowed_mentions=mentions)
            logger.info("Announced %s post: %s", feed.feed_id, title)
        except discord.HTTPException as exc:
            logger.error("Failed to announce %s post: %s", feed.feed_id, exc)

    async def _get_feed_state(self, feed_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT last_entry_id FROM blog_state WHERE feed_id = ?", (feed_id,)
        )
        return row["last_entry_id"] if row else None

    async def _set_feed_state(self, feed_id: str, entry_id: str) -> None:
        await self.db.execute(
            """INSERT INTO blog_state (feed_id, last_entry_id, checked_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(feed_id) DO UPDATE SET
                   last_entry_id = excluded.last_entry_id,
                   checked_at    = excluded.checked_at""",
            (feed_id, entry_id),
        )

    async def _import_legacy_blog_state(self) -> None:
        """Carry blog_data.json into the database once, so nothing is re-announced.

        The JSON file was read and written synchronously inside the async task,
        blocking the event loop, and a crash mid-write corrupted it.
        """
        # Derived from the configured database location rather than hardcoded,
        # so a run pointed at a throwaway database cannot consume the real file.
        legacy = Path(CONFIG.database_path).parent / "blog_data.json"

        # Checked before touching the filesystem: once feed state exists in the
        # database, the legacy file is irrelevant regardless of whether it is
        # still on disk.
        row = await self.db.fetch_one("SELECT COUNT(*) AS n FROM blog_state")
        if row is not None and row["n"] > 0:
            return

        try:
            # No exists() check: a missing file is just FileNotFoundError, which
            # avoids a blocking stat() call in an async function.
            raw = await asyncio.to_thread(legacy.read_text, encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not import %s: %s", legacy, exc)
            return

        imported = 0
        for feed_id, state in data.items():
            entry_id = state.get("last_id") if isinstance(state, dict) else None
            if entry_id:
                await self._set_feed_state(feed_id, entry_id)
                imported += 1

        logger.info("Imported %d feed states from %s", imported, legacy)
        try:
            await asyncio.to_thread(legacy.rename, legacy.with_suffix(".json.migrated"))
        except OSError as exc:
            logger.warning("Could not archive %s: %s", legacy, exc)

    # --- Rate limiting ---

    def _allow_rules_use(self, user_id: int) -> bool:
        """Sliding window of _RULES_MAX_USES per _RULES_WINDOW_SECONDS per user."""
        now = discord.utils.utcnow().timestamp()

        recent = [t for t in self._rules_usage.get(user_id, []) if now - t < _RULES_WINDOW_SECONDS]
        if len(recent) >= _RULES_MAX_USES:
            self._rules_usage[user_id] = recent
            self._prune_rules_usage(now)
            return False

        recent.append(now)
        self._rules_usage[user_id] = recent
        # Pruned after inserting, so the cap is a real ceiling on the dict size.
        self._prune_rules_usage(now)
        return True

    def _prune_rules_usage(self, now: float) -> None:
        """Drop expired entries so the tracking dict cannot grow without bound."""
        expired = [
            user_id
            for user_id, stamps in self._rules_usage.items()
            if not stamps or now - stamps[-1] >= _RULES_WINDOW_SECONDS
        ]
        for user_id in expired:
            del self._rules_usage[user_id]

        if len(self._rules_usage) > _RULES_TRACKED_USERS_CAP:
            oldest = sorted(self._rules_usage.items(), key=lambda item: item[1][-1])
            for user_id, _ in oldest[: len(self._rules_usage) - _RULES_TRACKED_USERS_CAP]:
                del self._rules_usage[user_id]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
