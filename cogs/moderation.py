"""Perpetual timeouts, the after-ban list, and the new-member role lifecycle.

Join-time work lives in handler methods called by MemberGateway, not in a
second on_member_join listener.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.checks import staff_only
from core.config import CONFIG
from core.errors import respond, respond_error

logger = logging.getLogger(__name__)

# Discord's hard cap for a single timeout call.
_MAX_TIMEOUT = timedelta(days=28)
# Refresh once the remaining time drops below this.
_REFRESH_THRESHOLD = timedelta(days=2)
# Pause between per-member API calls in the sweep tasks, to stay clear of the
# gateway rate limit when many members are affected at once.
_API_PACING_SECONDS = 0.5


class Moderation(commands.Cog):
    """Moderator tooling and the recurring enforcement sweeps."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        # Tasks start here, not in __init__, so they cannot run before the
        # database connection and migrations are ready.
        self.expire_new_member_roles.start()
        self.refresh_perpetual_timeouts.start()

    async def cog_unload(self) -> None:
        self.expire_new_member_roles.cancel()
        self.refresh_perpetual_timeouts.cancel()

    # --- Commands ---

    @app_commands.command(
        name="ptimeout", description="Perpetually timeout a user (moderators only)."
    )
    @app_commands.describe(member="The member to time out.", reason="Why they are being timed out.")
    @app_commands.guild_only()
    @staff_only()
    async def ptimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        invoker = interaction.user
        guild = interaction.guild
        if not isinstance(invoker, discord.Member) or guild is None:
            await respond_error(interaction, "This command only works inside the server.")
            return

        if member.id == invoker.id:
            await respond_error(interaction, "You cannot time yourself out.")
            return
        if member.bot:
            await respond_error(interaction, "Bots cannot be timed out.")
            return
        if member.id == guild.owner_id:
            await respond_error(interaction, "The server owner cannot be timed out.")
            return
        if invoker.id != guild.owner_id and member.top_role >= invoker.top_role:
            await respond_error(
                interaction,
                "You cannot time out someone whose highest role is at or above yours.",
            )
            return
        # Checking our own hierarchy up front turns a swallowed 403 into a clear message.
        if guild.me is not None and member.top_role >= guild.me.top_role:
            await respond_error(interaction, "My role is not high enough to time out that member.")
            return
        # Discord exempts administrators from timeouts entirely: the API accepts
        # the call and the member is never actually restricted. Saying so beats
        # reporting a success that does nothing.
        if member.guild_permissions.administrator:
            await respond_error(
                interaction,
                "That member has Administrator, and Discord exempts administrators "
                "from timeouts. Remove the permission first, or the timeout will "
                "have no effect.",
            )
            return

        try:
            await member.timeout(
                discord.utils.utcnow() + _MAX_TIMEOUT, reason=f"Perpetual timeout: {reason}"
            )
        except discord.Forbidden:
            await respond_error(interaction, "I do not have permission to time out that member.")
            return
        except discord.HTTPException as exc:
            logger.error("Failed to apply perpetual timeout to %s: %s", member.id, exc)
            await respond_error(interaction, "Discord rejected the timeout, please try again.")
            return

        now = discord.utils.utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO perpetual_timeouts
                   (user_id, reason, added_by, added_at, last_checked_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   reason          = excluded.reason,
                   added_by        = excluded.added_by,
                   last_checked_at = excluded.last_checked_at""",
            (member.id, reason, invoker.id, now, now),
        )
        logger.info("%s applied a perpetual timeout to %s: %s", invoker.id, member.id, reason)

        embed = discord.Embed(
            title="⏳ Perpetual Timeout Applied",
            description=f"{member.mention} has been placed in perpetual timeout.",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value="28 days, auto-renewing", inline=True)
        embed.set_footer(text=f"Applied by {invoker.name}")
        await respond(interaction, embed=embed, ephemeral=False)

    @app_commands.command(
        name="undoptimeout", description="Remove a user from perpetual timeout (moderators only)."
    )
    @app_commands.describe(member="The member to release.")
    @app_commands.guild_only()
    @staff_only()
    async def undoptimeout(self, interaction: discord.Interaction, member: discord.Member) -> None:
        removed = await self.db.execute(
            "DELETE FROM perpetual_timeouts WHERE user_id = ?", (member.id,)
        )
        if removed == 0:
            await respond_error(
                interaction, f"{member.mention} is not in the perpetual timeout list."
            )
            return

        if member.is_timed_out():
            try:
                await member.timeout(
                    None, reason=f"Perpetual timeout removed by {interaction.user.name}"
                )
            except discord.Forbidden:
                await respond(
                    interaction,
                    f"⚠️ Removed {member.mention} from the list, but I lack permission "
                    "to lift the active timeout.",
                    ephemeral=False,
                )
                return
            except discord.HTTPException as exc:
                logger.error("Failed to lift timeout for %s: %s", member.id, exc)

        logger.info("%s released %s from perpetual timeout", interaction.user.id, member.id)
        await respond(
            interaction, f"✅ Removed {member.mention} from perpetual timeout.", ephemeral=False
        )

    @app_commands.command(
        name="listptimeout", description="List all users currently in perpetual timeout."
    )
    @app_commands.guild_only()
    @staff_only()
    async def listptimeout(self, interaction: discord.Interaction) -> None:
        rows = await self.db.fetch_all(
            "SELECT user_id, reason, added_at FROM perpetual_timeouts ORDER BY added_at DESC"
        )
        if not rows:
            await respond(interaction, "✅ No users are currently in perpetual timeout.")
            return

        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="⏳ Perpetual Timeout List",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        # An embed accepts 25 fields; anything beyond that is summarised.
        for row in rows[:25]:
            embed.add_field(
                name=await self._describe_member(row["user_id"]),
                value=f"**Reason:** {row['reason']}\n**Date:** {_format_date(row['added_at'])}",
                inline=False,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)} entries")
        else:
            embed.set_footer(text=f"{len(rows)} total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- Join handlers, called by MemberGateway ---

    async def enforce_after_ban(self, member: discord.Member) -> bool:
        """Ban a rejoining user who is on the after-ban list. True when banned."""
        row = await self.db.fetch_one(
            "SELECT reason FROM after_ban_users WHERE user_id = ?", (member.id,)
        )
        if row is None:
            return False

        reason = row["reason"] or "on the after-ban list"
        try:
            await member.ban(reason=f"Automatic re-ban: {reason}")
        except discord.Forbidden:
            logger.error("Missing Ban Members permission, cannot re-ban %s", member.id)
            return False
        except discord.HTTPException as exc:
            logger.error("Failed to re-ban after-ban user %s: %s", member.id, exc)
            return False

        # Only clear the entry once the ban actually landed. The old code
        # deleted it inside the same try block, so a failed ban still dropped
        # the user from the list.
        await self.db.execute("DELETE FROM after_ban_users WHERE user_id = ?", (member.id,))
        logger.warning("Re-banned after-ban user %s (%s): %s", member, member.id, reason)
        return True

    async def assign_new_member_role(self, member: discord.Member) -> bool:
        role = member.guild.get_role(CONFIG.new_member_role_id)
        if role is None:
            logger.error("New member role %s not found", CONFIG.new_member_role_id)
            return False

        try:
            await member.add_roles(role, reason="New member probation period")
        except discord.Forbidden:
            logger.error("Missing Manage Roles permission, cannot assign new-member role")
            return False
        except discord.HTTPException as exc:
            logger.error("Failed to assign new-member role to %s: %s", member.id, exc)
            return False

        now = discord.utils.utcnow()
        expires = now + timedelta(hours=CONFIG.new_member_duration_hours)
        await self.db.execute(
            """INSERT INTO new_member_roles (user_id, guild_id, assigned_time, expires_time)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   guild_id      = excluded.guild_id,
                   assigned_time = excluded.assigned_time,
                   expires_time  = excluded.expires_time""",
            (member.id, member.guild.id, now.isoformat(), expires.isoformat()),
        )
        return True

    async def send_welcome_flow(self, member: discord.Member) -> None:
        """DM the welcome embed and the security notice. Failure is expected and fine."""
        welcome = discord.Embed(
            title="🕊️ Welcome to The Submission Server",
            description=(
                "Peace be upon you! We're honored to have you join us. "
                "Feel free to explore and engage with others."
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        welcome.add_field(
            name="📜 Choose Your Roles",
            value=f"Please choose your roles here: <#{CONFIG.role_select_channel_id}>",
            inline=False,
        )
        welcome.add_field(
            name="🛡️ Important Reminder",
            value=(
                "Some people may spread lies about this server. "
                "Remember Quran 17:36, verify all information for yourself."
            ),
            inline=False,
        )
        welcome.add_field(
            name="🙏 Blessing",
            value="May God guide you on the straight path and bless your time with us.",
            inline=False,
        )
        welcome.set_footer(text="Welcome to our God Alone community")

        try:
            await asyncio.sleep(2)
            await member.send(embed=welcome)
        except discord.Forbidden:
            logger.info("Cannot DM %s, direct messages are closed", member.id)
            return
        except discord.HTTPException as exc:
            logger.warning("Failed to DM welcome to %s: %s", member.id, exc)
            return

        await self._send_security_notice(member)

    async def _send_security_notice(self, member: discord.Member) -> None:
        warning = discord.Embed(
            title="⚠️ SECURITY ALERT",
            description=(
                f"**Avoid this user:**\n**Navid (navidfa)** — ID: {CONFIG.navid_warning_user_id}"
                "\n\n🚨 **Discord TOS Violator • Stalker • Troll**\n\n"
                "**If contacted:** Block immediately and report to moderators."
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        warning.set_footer(text="Security Alert")

        attachment: discord.File | None = None
        try:
            attachment = discord.File(CONFIG.navid_warning_image, filename="navid_warning.webp")
            warning.set_image(url="attachment://navid_warning.webp")
        except OSError as exc:
            # A missing image must not cost the member the whole notice.
            logger.error(
                "Security notice image unavailable (%s): %s", CONFIG.navid_warning_image, exc
            )

        try:
            await asyncio.sleep(3)
            if attachment is not None:
                await member.send(embed=warning, file=attachment)
            else:
                await member.send(embed=warning)
        except discord.Forbidden:
            logger.info("Cannot DM security notice to %s", member.id)
        except discord.HTTPException as exc:
            logger.warning("Failed to DM security notice to %s: %s", member.id, exc)

    # --- Tasks ---

    @tasks.loop(minutes=10)
    async def expire_new_member_roles(self) -> None:
        """Strip the new-member role once its window has passed."""
        rows = await self.db.fetch_all(
            "SELECT user_id, guild_id FROM new_member_roles WHERE expires_time <= ?",
            (discord.utils.utcnow().isoformat(),),
        )
        if not rows:
            return

        logger.info("Expiring the new-member role for %d member(s)", len(rows))
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            member = guild.get_member(row["user_id"]) if guild else None
            role = guild.get_role(CONFIG.new_member_role_id) if guild else None

            if member is not None and role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="New member period ended")
                    await asyncio.sleep(_API_PACING_SECONDS)
                except discord.Forbidden:
                    logger.error(
                        "Missing Manage Roles permission, keeping row for %s", row["user_id"]
                    )
                    continue
                except discord.HTTPException as exc:
                    logger.error(
                        "Failed to remove new-member role from %s: %s", row["user_id"], exc
                    )
                    continue

            # Reached for members who left, or whose role is already gone.
            await self.db.execute(
                "DELETE FROM new_member_roles WHERE user_id = ?", (row["user_id"],)
            )

    @tasks.loop(hours=1)
    async def refresh_perpetual_timeouts(self) -> None:
        """Top up timeouts that are approaching expiry, and prune stale rows.

        Ran every 5 minutes previously, which is roughly 8,000 times more often
        than a 28-day timeout requires and never cleaned up departed members.
        """
        rows = await self.db.fetch_all("SELECT user_id, reason FROM perpetual_timeouts")
        if not rows:
            return

        guild = self.bot.get_guild(CONFIG.guild_id)
        if guild is None:
            logger.warning("Guild %s unavailable, skipping timeout refresh", CONFIG.guild_id)
            return

        now = discord.utils.utcnow()
        refreshed = 0
        for row in rows:
            user_id = row["user_id"]
            member = guild.get_member(user_id)
            if member is None:
                # Not in the guild: the entry stays so it applies on rejoin, but
                # we record the check so it is visibly not being re-attempted.
                await self._touch_timeout(user_id, now)
                continue

            expiry = member.timed_out_until
            if expiry is not None and expiry - now > _REFRESH_THRESHOLD:
                continue

            try:
                await member.timeout(
                    now + _MAX_TIMEOUT, reason=f"Perpetual timeout: {row['reason']}"
                )
                refreshed += 1
                await asyncio.sleep(_API_PACING_SECONDS)
            except discord.Forbidden:
                logger.error(
                    "Cannot refresh perpetual timeout for %s, insufficient permissions", user_id
                )
                continue
            except discord.HTTPException as exc:
                logger.error("Failed to refresh timeout for %s: %s", user_id, exc)
                continue

            await self._touch_timeout(user_id, now)

        if refreshed:
            logger.info("Refreshed %d perpetual timeout(s)", refreshed)

    @expire_new_member_roles.before_loop
    @refresh_perpetual_timeouts.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    @expire_new_member_roles.error
    @refresh_perpetual_timeouts.error
    async def _task_error(self, error: BaseException) -> None:
        # tasks.loop stops permanently on an unhandled error unless this exists.
        logger.exception("Moderation background task failed", exc_info=error)

    # --- Helpers ---

    async def _touch_timeout(self, user_id: int, when: datetime) -> None:
        await self.db.execute(
            "UPDATE perpetual_timeouts SET last_checked_at = ? WHERE user_id = ?",
            (when.isoformat(), user_id),
        )

    async def _describe_member(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.NotFound:
                return f"Deleted account (`{user_id}`)"
            except discord.HTTPException:
                return f"Unknown user (`{user_id}`)"
        return f"{user.name} (`{user_id}`)"


def _format_date(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d")
    except ValueError:
        return "Unknown"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
