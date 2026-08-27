"""The single on_member_join pipeline.

Security and Moderation each used to register their own on_member_join listener,
so join handling ran in nondeterministic cog-load order: a user on the after-ban
list could be banned by one listener while the other concurrently granted roles
and wrote database rows. This cog owns the only listener and calls into the
others in a defined order, stopping as soon as the member has been removed.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import CONFIG

logger = logging.getLogger(__name__)


class MemberGateway(commands.Cog):
    """Runs every join-time action in one deterministic sequence."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        # The old Security listener had no guild guard, so it acted on joins in
        # any guild the bot happened to be in.
        if member.guild.id != CONFIG.guild_id:
            return

        security = self.bot.get_cog("Security")
        moderation = self.bot.get_cog("Moderation")
        if security is None or moderation is None:
            logger.error(
                "Join pipeline incomplete (security=%s, moderation=%s), skipping %s",
                security is not None,
                moderation is not None,
                member.id,
            )
            return

        logger.info("Processing join for %s (%s)", member, member.id)

        # 1. Unauthorised bots leave immediately; nothing else applies to them.
        if await security.check_unauthorized_bot(member):  # type: ignore[attr-defined]
            return
        if member.bot:
            return

        # 2. A user on the after-ban list is removed before any role or DM work.
        if await moderation.enforce_after_ban(member):  # type: ignore[attr-defined]
            return

        # 3. Throwaway accounts are flagged and restricted, but still proceed:
        #    the new-member role and welcome message apply to them too.
        await security.flag_suspicious_account(member)  # type: ignore[attr-defined]

        # 4. Probation role, then the welcome DMs last because they sleep.
        await moderation.assign_new_member_role(member)  # type: ignore[attr-defined]
        await moderation.send_welcome_flow(member)  # type: ignore[attr-defined]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberGateway(bot))
