"""Bot whitelisting, suspicious-account flagging, and new-member link blocking.

The join handlers here are called by the MemberGateway cog rather than being
wired to on_member_join directly, so that join handling has one deterministic
order instead of two cogs racing.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import STAFF_DEFAULT_PERMISSIONS, is_staff, is_staff_member
from core.config import CONFIG
from core.errors import respond, respond_error

logger = logging.getLogger(__name__)

# The old pattern was `https?://\S+`, which missed bare invites and bare
# hostnames. This also catches www-prefixed links, discord invites, and common
# TLDs without a scheme.
_LINK_PATTERN = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\bdiscord(?:app)?\.(?:gg|com/invite)/\S+"
    r"|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"\.(?:com|net|org|io|gg|me|xyz|co|ru|tv|info|link|app|dev|to|cc|shop|site|online)"
    r"\b(?:/\S*)?",
    re.IGNORECASE,
)

_WARNING_LIFETIME_SECONDS = 30.0


def contains_link(content: str) -> bool:
    """True when the message body contains something a member could click."""
    return _LINK_PATTERN.search(content) is not None


class Security(commands.Cog):
    """Guards the server against unauthorised bots and throwaway accounts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]
        self._whitelist: set[int] = set()

    async def cog_load(self) -> None:
        await self.refresh_whitelist()

    async def refresh_whitelist(self) -> None:
        rows = await self.db.fetch_all("SELECT bot_id FROM whitelist")
        self._whitelist = {row["bot_id"] for row in rows}
        logger.info("Loaded %d whitelisted bot IDs", len(self._whitelist))

    # --- Commands ---

    whitelist_group = app_commands.Group(
        name="botwhitelist",
        description="Manage the bot whitelist",
        default_permissions=STAFF_DEFAULT_PERMISSIONS,
        guild_only=True,
    )

    @whitelist_group.command(name="add", description="Add a bot ID to the whitelist.")
    @app_commands.describe(bot_id="The bot's Discord user ID (numbers only).")
    @is_staff()
    async def whitelist_add(self, interaction: discord.Interaction, bot_id: str) -> None:
        parsed = _parse_snowflake(bot_id)
        if parsed is None:
            await respond_error(interaction, "Provide a valid Discord ID (numbers only).")
            return

        if parsed in self._whitelist:
            await respond(interaction, f"ℹ️ `{parsed}` is already whitelisted.")
            return

        await self.db.execute("INSERT OR IGNORE INTO whitelist (bot_id) VALUES (?)", (parsed,))
        self._whitelist.add(parsed)
        logger.info("%s whitelisted bot %s", interaction.user.id, parsed)
        await respond(interaction, f"✅ Bot ID `{parsed}` added to the whitelist.", ephemeral=False)

    @whitelist_group.command(name="remove", description="Remove a bot ID from the whitelist.")
    @app_commands.describe(bot_id="The bot's Discord user ID (numbers only).")
    @is_staff()
    async def whitelist_remove(self, interaction: discord.Interaction, bot_id: str) -> None:
        parsed = _parse_snowflake(bot_id)
        if parsed is None:
            await respond_error(interaction, "Provide a valid Discord ID (numbers only).")
            return

        removed = await self.db.execute("DELETE FROM whitelist WHERE bot_id = ?", (parsed,))
        self._whitelist.discard(parsed)
        if removed == 0:
            await respond(interaction, f"ℹ️ `{parsed}` was not on the whitelist.")
            return

        logger.info("%s removed bot %s from the whitelist", interaction.user.id, parsed)
        await respond(
            interaction, f"🗑️ Bot ID `{parsed}` removed from the whitelist.", ephemeral=False
        )

    @whitelist_group.command(name="list", description="List all whitelisted bots.")
    @is_staff()
    async def whitelist_list(self, interaction: discord.Interaction) -> None:
        if not self._whitelist:
            await respond(interaction, "The whitelist is empty.")
            return

        await interaction.response.defer(ephemeral=True)
        lines: list[str] = []
        for bot_id in sorted(self._whitelist):
            lines.append(f"• `{bot_id}` ({await self._describe_user(bot_id)})")

        embed = discord.Embed(
            title="🤖 Whitelisted Bots",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(self._whitelist)} total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- Join handlers, called by MemberGateway ---

    async def check_unauthorized_bot(self, member: discord.Member) -> bool:
        """Kick a bot that is not on the whitelist. True when the bot was kicked."""
        if not member.bot or member.id in self._whitelist:
            return False

        try:
            await member.kick(reason="Unauthorized bot (not on whitelist)")
        except discord.Forbidden:
            logger.error(
                "Missing Kick Members permission, cannot remove unauthorized bot %s",
                member.id,
            )
            return False
        except discord.HTTPException as exc:
            logger.error("Failed to kick unauthorized bot %s: %s", member.id, exc)
            return False

        logger.warning("Kicked unauthorized bot %s (%s)", member, member.id)
        await self._log_to_staff(
            member.guild,
            content=f"🤖 **Unauthorized Bot Kicked:** {member.mention} (`{member.id}`)",
        )
        return True

    async def flag_suspicious_account(self, member: discord.Member) -> bool:
        """Flag and restrict an account created shortly before joining."""
        max_age_hours = CONFIG.suspicious_account_max_age_hours
        age = discord.utils.utcnow() - member.created_at
        age_hours = age.total_seconds() / 3600
        if age_hours >= max_age_hours:
            return False

        role_assigned = await self._assign_unverified_role(member)
        notified = await self._notify_staff_of_suspicious(member, age_hours, role_assigned)

        # joined_at is Optional on Member; the old code dereferenced it blindly
        # and the resulting AttributeError was swallowed, losing the record.
        joined_at = member.joined_at or discord.utils.utcnow()

        await self.db.execute(
            """INSERT INTO suspicious_accounts
                   (user_id, guild_id, username, display_name,
                    account_created_at, joined_at, flagged_at,
                    unverified_role_assigned, staff_notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   guild_id                 = excluded.guild_id,
                   username                 = excluded.username,
                   display_name             = excluded.display_name,
                   account_created_at       = excluded.account_created_at,
                   joined_at                = excluded.joined_at,
                   flagged_at               = excluded.flagged_at,
                   unverified_role_assigned = excluded.unverified_role_assigned,
                   staff_notified           = excluded.staff_notified""",
            (
                member.id,
                member.guild.id,
                member.name,
                member.display_name,
                member.created_at.isoformat(),
                joined_at.isoformat(),
                discord.utils.utcnow().isoformat(),
                int(role_assigned),
                int(notified),
            ),
        )
        logger.warning(
            "Flagged suspicious account %s (%s), age %.1fh, role assigned=%s",
            member,
            member.id,
            age_hours,
            role_assigned,
        )
        return True

    # --- Listeners ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Delete links posted by members still inside the new-member window."""
        if message.author.bot or message.guild is None:
            return
        if message.guild.id != CONFIG.guild_id:
            return
        if not isinstance(message.author, discord.Member):
            return
        # Staff keep posting rights even if they hold the new-member role.
        if is_staff_member(message.author):
            return
        if not any(role.id == CONFIG.new_member_role_id for role in message.author.roles):
            return
        if not contains_link(message.content):
            return

        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            logger.error("Missing Manage Messages permission, cannot delete new-member link")
            return
        except discord.HTTPException as exc:
            logger.error("Failed to delete new-member link: %s", exc)
            return

        embed = discord.Embed(
            title="⚠️ Link Blocked",
            description=(
                f"{message.author.mention}, posting links is not allowed for new members "
                "for security purposes.\n\nThis restriction helps prevent spam and "
                "inappropriate content, and is lifted automatically once your new member "
                "phase is over, God willing."
            ),
            color=discord.Color.red(),
        )
        try:
            # Auto-expire so the channel is not left full of warnings.
            await message.channel.send(embed=embed, delete_after=_WARNING_LIFETIME_SECONDS)
        except discord.HTTPException as exc:
            logger.warning("Could not post link-blocked notice: %s", exc)

    # --- Helpers ---

    async def _assign_unverified_role(self, member: discord.Member) -> bool:
        role = member.guild.get_role(CONFIG.unverified_role_id)
        if role is None:
            logger.error(
                "Unverified role %s not found in guild %s",
                CONFIG.unverified_role_id,
                member.guild.id,
            )
            return False
        try:
            await member.add_roles(role, reason="Account created less than 24h before joining")
            return True
        except discord.Forbidden:
            logger.error("Missing Manage Roles permission, cannot restrict %s", member.id)
        except discord.HTTPException as exc:
            logger.error("Failed to add unverified role to %s: %s", member.id, exc)
        return False

    async def _notify_staff_of_suspicious(
        self, member: discord.Member, age_hours: float, role_assigned: bool
    ) -> bool:
        embed = discord.Embed(
            title="🚨 Suspicious Account Detected",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        embed.add_field(name="Account Age", value=f"{age_hours:.1f} hours", inline=True)
        embed.add_field(
            name="Action Taken",
            value=(
                f"Assigned <@&{CONFIG.unverified_role_id}>"
                if role_assigned
                else "⚠️ Failed to assign the Unverified role, check bot permissions"
            ),
            inline=False,
        )
        return await self._log_to_staff(member.guild, embed=embed)

    async def _log_to_staff(
        self,
        guild: discord.Guild,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> bool:
        channel = guild.get_channel(CONFIG.staff_logs_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logger.error(
                "Staff logs channel %s is missing or not messageable",
                CONFIG.staff_logs_channel_id,
            )
            return False
        try:
            await channel.send(content=content, embed=embed)
            return True
        except discord.HTTPException as exc:
            logger.error("Failed to write to staff logs: %s", exc)
            return False

    async def _describe_user(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.NotFound:
                return "deleted or invalid account"
            except discord.HTTPException as exc:
                logger.warning("Could not fetch user %s: %s", user_id, exc)
                return "unknown"
        return user.name


def _parse_snowflake(raw: str) -> int | None:
    """Parse a Discord ID, tolerating a <@123> style mention."""
    cleaned = raw.strip().strip("<@!>")
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    # Discord snowflakes are 17 to 20 digits; reject obvious typos.
    return value if 10**16 <= value < 10**20 else None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Security(bot))
