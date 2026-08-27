"""Entry point: configure logging, validate config, open the database, run the bot."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import signal
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

from core.checks import (  # noqa: E402
    GuildOnlyError,
    NotAdminError,
    NotOwnerError,
    NotStaffError,
)
from core.config import CONFIG, ConfigError  # noqa: E402
from core.database import Database, DatabaseError  # noqa: E402
from core.errors import UserFacingError, respond_error  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("bot")

# How often the liveness file is refreshed. The container healthcheck
# tolerates three times this before reporting unhealthy.
_HEARTBEAT_SECONDS = 60

# Upper bound on how long discord.py's own shutdown may take before we stop
# waiting on it. Without this the process can sit until the container runtime
# sends SIGKILL, which loses the clean database close entirely.
_CLIENT_CLOSE_TIMEOUT = 10.0

# Hard ceiling on the whole shutdown, kept under the compose stop_grace_period
# so the process exits on its own rather than being killed.
_SHUTDOWN_TIMEOUT = 20.0

EXTENSIONS = (
    "cogs.security",
    "cogs.moderation",
    "cogs.membership",
    "cogs.events",
    "cogs.admin",
    "cogs.debate",
    "cogs.archive",
)


class SubmissionBot(commands.Bot):
    """The bot, owning the shared database handle."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            max_messages=5000,
            heartbeat_timeout=120.0,
            guild_ready_timeout=60.0,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.db = Database(CONFIG.database_path)
        self._heartbeat_path = Path(CONFIG.log_dir) / "heartbeat"

    async def setup_hook(self) -> None:
        await self.db.connect()
        logger.info(
            "Database ready at %s (schema version %s)",
            CONFIG.database_path,
            await self.db.schema_version(),
        )

        # A cog that cannot load is fatal: running with half the moderation
        # features silently disabled is worse than not starting.
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            logger.info("Loaded extension %s", extension)

        self.tree.on_error = self.on_tree_error
        await self._sync_commands()
        self.heartbeat.start()

    def _command_fingerprint(self, guild: discord.Object | None) -> str:
        """Hash the command payloads that would be uploaded."""
        payloads = [
            command.to_dict(self.tree)  # type: ignore[arg-type]
            for command in self.tree.get_commands(guild=guild)
        ]
        blob = json.dumps(payloads, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def _sync_commands(self) -> None:
        """Sync only when the command set actually changed.

        Discord allows 200 command creates per day per guild, and a bulk
        overwrite counts. start_bot.bat restarts on crash, so an unconditional
        sync on every startup could exhaust that budget during a crash loop and
        leave the guild with no commands. Guild-scoped syncs also apply instantly,
        unlike global ones which propagate lazily.
        """
        scope = "global" if CONFIG.global_command_sync else str(CONFIG.guild_id)
        guild = None if CONFIG.global_command_sync else discord.Object(id=CONFIG.guild_id)

        if guild is not None:
            self.tree.copy_global_to(guild=guild)

        try:
            fingerprint = self._command_fingerprint(guild)
        except Exception:
            logger.warning("Could not fingerprint commands, syncing unconditionally")
            fingerprint = None

        if fingerprint is not None:
            row = await self.db.fetch_one(
                "SELECT fingerprint FROM command_sync_state WHERE scope = ?", (scope,)
            )
            if row is not None and row["fingerprint"] == fingerprint:
                logger.info("Commands unchanged for scope %s, skipping sync", scope)
                return

        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            logger.error("Command sync failed: %s", exc)
            return

        logger.info("Synced %d command(s) to scope %s", len(synced), scope)
        if fingerprint is not None:
            await self.db.execute(
                """INSERT INTO command_sync_state (scope, fingerprint, synced_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(scope) DO UPDATE SET
                       fingerprint = excluded.fingerprint,
                       synced_at   = excluded.synced_at""",
                (scope, fingerprint),
            )

    async def close(self) -> None:
        """Shut down in bounded time, closing our own resources regardless.

        discord.py's close can block on draining its HTTP session, so it is
        given a deadline. The database is closed afterwards either way: it is
        ours, and leaving it open on the way out is worse than a noisy log.
        """
        self.heartbeat.cancel()
        try:
            await asyncio.wait_for(super().close(), timeout=_CLIENT_CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "Discord client did not close within %.0fs, continuing shutdown",
                _CLIENT_CLOSE_TIMEOUT,
            )
        except Exception:
            logger.exception("Error while closing the Discord client")

        await self.db.close()
        logger.info("Database connection closed")

    @tasks.loop(seconds=_HEARTBEAT_SECONDS)
    async def heartbeat(self) -> None:
        """Touch a file so a container healthcheck can see the loop is alive.

        Log freshness is not a liveness signal: a quiet bot legitimately writes
        no logs for hours. This goes stale only if the event loop stops running.
        """
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            # latency is NaN until the first gateway heartbeat lands, and
            # "nans" in a health file reads as a bug.
            latency = self.latency
            latency_text = f"{latency * 1000:.0f}ms" if math.isfinite(latency) else "unknown"
            self._heartbeat_path.write_text(
                f"{discord.utils.utcnow().isoformat()} latency={latency_text}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write the heartbeat file: %s", exc)

    @heartbeat.before_loop
    async def _heartbeat_ready(self) -> None:
        await self.wait_until_ready()

    @heartbeat.error
    async def _heartbeat_error(self, error: BaseException) -> None:
        logger.exception("Heartbeat task failed", exc_info=error)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        logger.info("Connected to %d guild(s)", len(self.guilds))
        if self.get_guild(CONFIG.guild_id) is None:
            logger.warning(
                "Configured GUILD_ID %s is not among the connected guilds",
                CONFIG.guild_id,
            )

    async def on_resumed(self) -> None:
        logger.info("Gateway session resumed")

    async def on_error(self, event_method: str, /, *args: object, **kwargs: object) -> None:
        logger.exception("Unhandled exception in event %s", event_method)

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, NotStaffError | NotAdminError | NotOwnerError | GuildOnlyError):
            await respond_error(interaction, str(error))
            return

        if isinstance(error, app_commands.MissingPermissions):
            await respond_error(interaction, "You do not have permission to use this command.")
            return

        if isinstance(error, app_commands.CommandOnCooldown):
            await respond_error(
                interaction,
                f"That command is on cooldown, try again in {error.retry_after:.0f}s.",
            )
            return

        if isinstance(error, app_commands.CheckFailure):
            await respond_error(interaction, "You cannot use this command here.")
            return

        original = getattr(error, "original", error)
        if isinstance(original, UserFacingError):
            await respond_error(interaction, original.message)
            return

        logger.exception(
            "Command /%s failed for %s",
            getattr(interaction.command, "name", "unknown"),
            interaction.user.id,
            exc_info=error,
        )
        await respond_error(interaction, "An unexpected error occurred. Staff have been notified.")


def _install_shutdown_handlers(stop: asyncio.Event) -> None:
    """Set an event on SIGTERM or Ctrl-C. `main` owns the actual shutdown.

    `docker stop` sends SIGTERM, whose default action ends the process at once,
    skipping the path that closes the database. The handler only signals: it
    must not start a shutdown of its own, or it races the one in main and both
    can block before either logs anything. Signal handlers are unavailable on
    the Windows event loop, so this is best-effort there.
    """
    loop = asyncio.get_running_loop()

    def request_shutdown(signal_name: str) -> None:
        logger.info("Received %s, shutting down", signal_name)
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, request_shutdown, name)


async def main() -> int:
    setup_logging(log_dir=CONFIG.log_dir, level=CONFIG.log_level)

    try:
        CONFIG.validate()
    except ConfigError as exc:
        logger.critical("%s", exc)
        return 2

    bot = SubmissionBot()
    stop = asyncio.Event()
    _install_shutdown_handlers(stop)

    # start() rather than run() so logging is already configured and the process
    # exits with a status code instead of waiting on stdin. Racing it against the
    # stop event means shutdown never depends on start() returning by itself.
    runner = asyncio.create_task(bot.start(CONFIG.token), name="bot-run")
    waiter = asyncio.create_task(stop.wait(), name="shutdown-signal")
    status = 0

    try:
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)

        if runner in done:
            # Surface whatever ended the run: a clean stop or a fatal error.
            runner.result()
        else:
            try:
                await asyncio.wait_for(bot.close(), timeout=_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    "Shutdown did not finish within %.0fs, exiting anyway",
                    _SHUTDOWN_TIMEOUT,
                )
    except discord.LoginFailure:
        logger.critical("Discord rejected the token. Rotate it and update .env")
        status = 2
    except DatabaseError as exc:
        logger.critical("Database unavailable: %s", exc)
        status = 3
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Interrupted")
    except Exception:
        logger.exception("Fatal error")
        status = 1
    finally:
        # Nothing here may block: the process must reach exit even if the
        # Discord client refuses to close.
        for task in (waiter, runner):
            task.cancel()
        await asyncio.gather(waiter, runner, return_exceptions=True)
        # Our own handle, closed regardless of what the client did.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bot.db.close(), timeout=5)
        logger.info("Shutdown complete")

    return status


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
