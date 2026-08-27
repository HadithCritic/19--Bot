"""Owner-only channel archiving.

Design constraints taken from the Discord docs:

- An interaction token is valid for 15 minutes and the initial response is due
  within 3 seconds, so a full-channel scrape cannot report through the
  interaction. The command acknowledges ephemerally, detaches the work into a
  background task, and reports progress by DM.
- Get Channel Messages returns at most 100 messages per request, newest first;
  discord.py's history() iterator handles the before/after paging. Reading
  history needs both VIEW_CHANNEL and READ_MESSAGE_HISTORY: without the latter
  the endpoint returns nothing rather than erroring, so both are checked up
  front.
- Forum and media channels contain only threads, so their posts are archived by
  walking active plus archived threads.
- Progress is delivered by editing one DM instead of sending many, to stay well
  inside the 50 requests per second global limit.
- Error code 50007 means the owner's DMs are closed. Since DMs are the only
  progress channel, that is checked before any work starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.checks import owner_only
from core.config import CONFIG
from core.errors import respond, respond_error

logger = logging.getLogger(__name__)

# Discord JSON error codes we treat as expected conditions.
_CANNOT_DM_USER = 50007

# Edit the progress DM at most this often, and at least this many messages
# apart, so a long run stays quiet on the API.
_PROGRESS_MIN_SECONDS = 12.0
_PROGRESS_EVERY_MESSAGES = 1000

# Concurrent attachment downloads. Kept low because these are large transfers,
# not API calls, and the bot must stay responsive while archiving.
_DOWNLOAD_CONCURRENCY = 3
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
_DOWNLOAD_CHUNK = 64 * 1024

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LENGTH = 80


def safe_name(raw: str, *, fallback: str = "unnamed") -> str:
    """Reduce arbitrary text to something safe as a single path segment."""
    cleaned = _UNSAFE_PATH_CHARS.sub("_", raw).strip("._-")
    cleaned = cleaned[:_MAX_NAME_LENGTH]
    return cleaned or fallback


def serialise_message(message: discord.Message) -> dict:
    """Flatten a message into a stable JSON record."""
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "type": str(message.type),
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "author": {
            "id": message.author.id,
            "name": message.author.name,
            "display_name": message.author.display_name,
            "bot": message.author.bot,
        },
        "content": message.content,
        "clean_content": message.clean_content,
        "pinned": message.pinned,
        "tts": message.tts,
        "reference": (message.reference.message_id if message.reference is not None else None),
        "attachments": [
            {
                "id": item.id,
                "filename": item.filename,
                "content_type": item.content_type,
                "size": item.size,
                "url": item.url,
                "description": item.description,
                "spoiler": item.is_spoiler(),
            }
            for item in message.attachments
        ],
        "embeds": [embed.to_dict() for embed in message.embeds],
        "stickers": [
            {"id": sticker.id, "name": sticker.name, "url": sticker.url}
            for sticker in message.stickers
        ],
        "reactions": [
            {"emoji": str(reaction.emoji), "count": reaction.count}
            for reaction in message.reactions
        ],
        "mentions": [user.id for user in message.mentions],
        "role_mentions": [role.id for role in message.role_mentions],
        "jump_url": message.jump_url,
    }


@dataclass
class ArchiveStats:
    """Running totals for one archive job."""

    messages: int = 0
    attachments_saved: int = 0
    attachments_skipped: int = 0
    attachment_bytes: int = 0
    channels_done: int = 0
    channels_total: int = 1
    errors: list[str] = field(default_factory=list)
    last_message_id: int | None = None

    def note_error(self, message: str) -> None:
        # Only the first few are worth reporting; the log has the rest.
        if len(self.errors) < 10:
            self.errors.append(message)


class ChannelArchiver:
    """Writes one channel's history to disk as JSONL plus saved attachments."""

    def __init__(
        self,
        *,
        destination: Path,
        session: aiohttp.ClientSession,
        include_attachments: bool,
        max_attachment_bytes: int,
        stats: ArchiveStats,
    ) -> None:
        self._destination = destination
        self._session = session
        self._include_attachments = include_attachments
        self._max_attachment_bytes = max_attachment_bytes
        self._stats = stats
        self._semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

    async def run(
        self,
        channel: discord.abc.Messageable,
        *,
        after_id: int | None,
        on_progress,
    ) -> None:
        label = getattr(channel, "name", "channel")
        folder = self._destination / safe_name(str(label))
        folder.mkdir(parents=True, exist_ok=True)
        attachments_dir = folder / "attachments"

        transcript = folder / "messages.jsonl"
        # Append so a resumed run extends the transcript rather than truncating.
        handle = transcript.open("a", encoding="utf-8")
        after = discord.Object(id=after_id) if after_id else None
        pending_downloads: list[asyncio.Task] = []

        try:
            async for message in channel.history(limit=None, oldest_first=True, after=after):
                handle.write(json.dumps(serialise_message(message), ensure_ascii=False))
                handle.write("\n")
                self._stats.messages += 1
                self._stats.last_message_id = message.id

                if self._include_attachments and message.attachments:
                    attachments_dir.mkdir(parents=True, exist_ok=True)
                    for attachment in message.attachments:
                        pending_downloads.append(
                            asyncio.create_task(
                                self._save_attachment(attachment, message.id, attachments_dir)
                            )
                        )

                # Drain finished downloads so the task list cannot grow without
                # bound on a channel with tens of thousands of attachments.
                if len(pending_downloads) >= 50:
                    await asyncio.gather(*pending_downloads, return_exceptions=True)
                    pending_downloads.clear()

                if self._stats.messages % 200 == 0:
                    handle.flush()
                    await on_progress()
        finally:
            if pending_downloads:
                await asyncio.gather(*pending_downloads, return_exceptions=True)
            handle.flush()
            handle.close()

    async def _save_attachment(
        self, attachment: discord.Attachment, message_id: int, folder: Path
    ) -> None:
        if attachment.size > self._max_attachment_bytes:
            self._stats.attachments_skipped += 1
            logger.info(
                "Skipping %s (%.1f MB exceeds the limit)",
                attachment.filename,
                attachment.size / 1_048_576,
            )
            return

        target = folder / f"{message_id}-{attachment.id}-{safe_name(attachment.filename)}"
        if target.exists() and target.stat().st_size == attachment.size:
            return  # already saved by an earlier run

        async with self._semaphore:
            try:
                # Streamed to disk so a large video does not sit in memory.
                async with self._session.get(attachment.url) as response:
                    if response.status != 200:
                        raise OSError(f"HTTP {response.status}")
                    temporary = target.with_suffix(target.suffix + ".part")
                    with temporary.open("wb") as out:
                        async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK):
                            out.write(chunk)
                    temporary.replace(target)
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                self._stats.attachments_skipped += 1
                self._stats.note_error(f"{attachment.filename}: {exc}")
                logger.warning("Failed to download %s: %s", attachment.url, exc)
                return

        self._stats.attachments_saved += 1
        self._stats.attachment_bytes += attachment.size


class Archive(commands.Cog):
    """Bulk export of a channel's history, restricted to the app owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]
        self._session: aiohttp.ClientSession | None = None
        self._running: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT)

    async def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- Command ---

    @app_commands.command(
        name="archive",
        description="Save a channel's full history to disk (bot owner only).",
    )
    @app_commands.describe(
        channel="Channel to archive. Defaults to the channel you run this in.",
        include_attachments="Download images and files as well as text. Default: yes.",
        include_threads="Also archive threads inside the channel. Default: yes.",
        restart="Ignore the previous run and archive from the beginning. Default: no.",
    )
    @app_commands.guild_only()
    @owner_only()
    async def archive(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.ForumChannel | discord.Thread | None = None,
        include_attachments: bool = True,
        include_threads: bool = True,
        restart: bool = False,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel | discord.ForumChannel | discord.Thread):
            await respond_error(interaction, "That channel type cannot be archived.")
            return

        if target.id in self._running:
            await respond_error(interaction, f"{target.mention} is already being archived.")
            return

        problem = self._permission_problem(target)
        if problem is not None:
            await respond_error(interaction, problem)
            return

        # Progress is DM-only, so confirm the DM channel opens before starting.
        dm_message = await self._open_progress_dm(interaction.user, target)
        if dm_message is None:
            await respond_error(
                interaction,
                "I cannot DM you, so I have nowhere to report progress. "
                "Enable direct messages from server members and try again.",
            )
            return

        # Ephemeral, so nothing appears in the channel for anyone else.
        await respond(
            interaction,
            f"📦 Archiving {target.mention}. Progress will arrive in your DMs.",
        )

        # Detached: the run will outlive the 15 minute interaction token.
        task = asyncio.create_task(
            self._run_job(
                target,
                dm_message,
                include_attachments=include_attachments,
                include_threads=include_threads,
                restart=restart,
            ),
            name=f"archive-{target.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- Job ---

    async def _run_job(
        self,
        channel: discord.TextChannel | discord.ForumChannel | discord.Thread,
        dm_message: discord.Message,
        *,
        include_attachments: bool,
        include_threads: bool,
        restart: bool,
    ) -> None:
        self._running.add(channel.id)
        stats = ArchiveStats()
        started = discord.utils.utcnow()
        destination = (
            Path(CONFIG.archive_dir)
            / safe_name(str(channel.guild.id))
            / f"{safe_name(channel.name)}-{channel.id}"
        )

        try:
            destination.mkdir(parents=True, exist_ok=True)
            after_id = None if restart else await self._resume_point(channel.id)
            await self._record_start(channel, started)

            targets = await self._collect_targets(channel, include_threads)
            stats.channels_total = len(targets)

            if self._session is None:
                raise RuntimeError("HTTP session unavailable")

            archiver = ChannelArchiver(
                destination=destination,
                session=self._session,
                include_attachments=include_attachments,
                max_attachment_bytes=CONFIG.archive_attachment_max_mb * 1_048_576,
                stats=stats,
            )
            reporter = _ProgressReporter(dm_message, channel, stats, started)
            await reporter.update(force=True)

            for sub_target in targets:
                # Resume only applies to the root channel; threads are archived
                # in full because their own progress is not tracked separately.
                sub_after = after_id if sub_target.id == channel.id else None
                try:
                    await archiver.run(sub_target, after_id=sub_after, on_progress=reporter.update)
                except discord.Forbidden:
                    stats.note_error(f"no access to {getattr(sub_target, 'name', '?')}")
                    logger.warning("Lost access to %s during archive", sub_target.id)
                except discord.HTTPException as exc:
                    stats.note_error(f"{getattr(sub_target, 'name', '?')}: {exc}")
                    logger.error("HTTP error archiving %s: %s", sub_target.id, exc)
                stats.channels_done += 1
                await reporter.update(force=True)

            await self._write_manifest(destination, channel, stats, started)
            await self._record_finish(channel.id, stats, status="complete")
            await reporter.finish(destination)
            logger.info(
                "Archived %s: %d messages, %d attachments",
                channel.name,
                stats.messages,
                stats.attachments_saved,
            )

        except asyncio.CancelledError:
            await self._record_finish(channel.id, stats, status="cancelled")
            with contextlib.suppress(discord.HTTPException):
                await dm_message.reply(
                    f"⚠️ Archive of **{channel.name}** was cancelled after "
                    f"{stats.messages:,} messages. Re-run `/archive` to resume."
                )
            raise
        except Exception as exc:
            logger.exception("Archive of %s failed", channel.id)
            await self._record_finish(channel.id, stats, status="failed")
            with contextlib.suppress(discord.HTTPException):
                await dm_message.reply(
                    f"❌ Archive of **{channel.name}** failed after "
                    f"{stats.messages:,} messages.\n```{type(exc).__name__}: {exc}```"
                )
        finally:
            self._running.discard(channel.id)

    async def _collect_targets(
        self,
        channel: discord.TextChannel | discord.ForumChannel | discord.Thread,
        include_threads: bool,
    ) -> list[discord.abc.Messageable]:
        """The channel itself, plus its threads when asked.

        Forum and media channels hold no messages of their own, so for those the
        threads are the entire archive.
        """
        targets: list[discord.abc.Messageable] = []
        if not isinstance(channel, discord.ForumChannel):
            targets.append(channel)

        if isinstance(channel, discord.Thread) or not include_threads:
            return targets or [channel]

        seen = {getattr(item, "id", 0) for item in targets}
        for thread in channel.threads:  # active, from the gateway cache
            if thread.id not in seen:
                targets.append(thread)
                seen.add(thread.id)

        # Archived threads are not cached and must be paged from the API.
        for archived in (
            channel.archived_threads(limit=None, private=False),
            channel.archived_threads(limit=None, private=True, joined=False),
        ):
            try:
                async for thread in archived:
                    if thread.id not in seen:
                        targets.append(thread)
                        seen.add(thread.id)
            except discord.Forbidden:
                # Listing private archived threads needs Manage Threads.
                logger.info("Cannot list some archived threads in %s", channel.id)
            except discord.HTTPException as exc:
                logger.warning("Failed listing archived threads in %s: %s", channel.id, exc)

        logger.info("Archive target %s expanded to %d channel(s)", channel.id, len(targets))
        return targets

    # --- Persistence ---

    async def _resume_point(self, channel_id: int) -> int | None:
        row = await self.db.fetch_one(
            "SELECT last_message_id FROM archive_runs WHERE channel_id = ?", (channel_id,)
        )
        return row["last_message_id"] if row else None

    async def _record_start(self, channel, started: datetime) -> None:
        await self.db.execute(
            """INSERT INTO archive_runs
                   (channel_id, guild_id, channel_name, started_at, status)
               VALUES (?, ?, ?, ?, 'running')
               ON CONFLICT(channel_id) DO UPDATE SET
                   channel_name = excluded.channel_name,
                   started_at   = excluded.started_at,
                   completed_at = NULL,
                   status       = 'running'""",
            (channel.id, channel.guild.id, channel.name, started.isoformat()),
        )

    async def _record_finish(self, channel_id: int, stats: ArchiveStats, *, status: str) -> None:
        await self.db.execute(
            """UPDATE archive_runs
               SET last_message_id  = COALESCE(?, last_message_id),
                   message_count    = message_count + ?,
                   attachment_count = attachment_count + ?,
                   completed_at     = ?,
                   status           = ?
               WHERE channel_id = ?""",
            (
                stats.last_message_id,
                stats.messages,
                stats.attachments_saved,
                discord.utils.utcnow().isoformat(),
                status,
                channel_id,
            ),
        )

    async def _write_manifest(
        self, destination: Path, channel, stats: ArchiveStats, started: datetime
    ) -> None:
        manifest = {
            "guild": {"id": channel.guild.id, "name": channel.guild.name},
            "channel": {"id": channel.id, "name": channel.name, "type": str(channel.type)},
            "started_at": started.isoformat(),
            "completed_at": discord.utils.utcnow().isoformat(),
            "messages": stats.messages,
            "attachments_saved": stats.attachments_saved,
            "attachments_skipped": stats.attachments_skipped,
            "attachment_bytes": stats.attachment_bytes,
            "channels_archived": stats.channels_done,
            "last_message_id": stats.last_message_id,
            "errors": stats.errors,
        }
        path = destination / "manifest.json"
        await asyncio.to_thread(
            path.write_text, json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8"
        )

    # --- Helpers ---

    @staticmethod
    def _permission_problem(channel) -> str | None:
        """Reading history needs View Channel and Read Message History.

        Without the latter the endpoint returns an empty list rather than an
        error, which would look like an empty channel.
        """
        me = channel.guild.me
        if me is None:
            return "I cannot resolve my own member object in this server."
        perms = channel.permissions_for(me)
        missing = [
            name
            for name, granted in (
                ("View Channel", perms.view_channel),
                ("Read Message History", perms.read_message_history),
            )
            if not granted
        ]
        if missing:
            return f"I am missing {' and '.join(missing)} in that channel."
        return None

    async def _open_progress_dm(
        self, user: discord.User | discord.Member, channel
    ) -> discord.Message | None:
        embed = discord.Embed(
            title="📦 Archive queued",
            description=f"Preparing to archive **#{channel.name}**...",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        try:
            return await user.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Owner %s has DMs closed, archive refused", user.id)
            return None
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) == _CANNOT_DM_USER:
                return None
            logger.error("Could not open the progress DM: %s", exc)
            return None


class _ProgressReporter:
    """Edits a single DM in place instead of sending a stream of messages."""

    def __init__(
        self,
        message: discord.Message,
        channel,
        stats: ArchiveStats,
        started: datetime,
    ) -> None:
        self._message = message
        self._channel = channel
        self._stats = stats
        self._started = started
        self._last_edit = 0.0
        self._last_count = 0

    async def update(self, *, force: bool = False) -> None:
        now = discord.utils.utcnow().timestamp()
        due_by_time = now - self._last_edit >= _PROGRESS_MIN_SECONDS
        due_by_count = self._stats.messages - self._last_count >= _PROGRESS_EVERY_MESSAGES
        if not force and not (due_by_time and due_by_count):
            return

        self._last_edit = now
        self._last_count = self._stats.messages
        with contextlib.suppress(discord.HTTPException):
            await self._message.edit(embed=self._build_embed(done=False))

    async def finish(self, destination: Path) -> None:
        embed = self._build_embed(done=True)
        embed.add_field(name="Saved to", value=f"`{destination}`", inline=False)
        with contextlib.suppress(discord.HTTPException):
            await self._message.edit(embed=embed)
        with contextlib.suppress(discord.HTTPException):
            # A separate message so the completion actually pings the DM.
            await self._message.reply(
                f"✅ Archive of **#{self._channel.name}** complete: "
                f"{self._stats.messages:,} messages, "
                f"{self._stats.attachments_saved:,} attachments."
            )

    def _build_embed(self, *, done: bool) -> discord.Embed:
        elapsed = (discord.utils.utcnow() - self._started).total_seconds()
        rate = self._stats.messages / elapsed if elapsed > 0 else 0.0

        embed = discord.Embed(
            title="✅ Archive complete" if done else "📦 Archiving...",
            description=f"**#{self._channel.name}** in {self._channel.guild.name}",
            color=discord.Color.green() if done else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Messages", value=f"{self._stats.messages:,}", inline=True)
        embed.add_field(
            name="Attachments",
            value=(
                f"{self._stats.attachments_saved:,} saved"
                + (
                    f", {self._stats.attachments_skipped:,} skipped"
                    if self._stats.attachments_skipped
                    else ""
                )
            ),
            inline=True,
        )
        embed.add_field(
            name="Downloaded",
            value=f"{self._stats.attachment_bytes / 1_048_576:.1f} MB",
            inline=True,
        )
        if self._stats.channels_total > 1:
            embed.add_field(
                name="Channels",
                value=f"{self._stats.channels_done}/{self._stats.channels_total}",
                inline=True,
            )
        embed.add_field(name="Elapsed", value=_format_duration(elapsed), inline=True)
        embed.add_field(name="Rate", value=f"{rate:.0f} msg/s", inline=True)
        if self._stats.errors:
            embed.add_field(
                name=f"⚠️ Issues ({len(self._stats.errors)})",
                value="\n".join(f"• {item}" for item in self._stats.errors[:5])[:1000],
                inline=False,
            )
        return embed


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Archive(bot))
