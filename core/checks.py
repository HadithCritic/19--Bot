"""One permission mechanism for every command.

The old code used five different approaches: default_permissions, inline
has_permissions checks, manual role-ID comparison, a hardcoded user-ID bypass,
and no check at all. Commands now pair a default_permissions hint (which greys
the command out in the Discord client) with one of the checks below (which
actually enforces it, including for users the client would have allowed).
"""

from __future__ import annotations

import discord
from discord import app_commands

from core.config import CONFIG


class NotStaffError(app_commands.CheckFailure):
    """Raised when the invoker is neither a moderator nor an administrator."""


class NotAdminError(app_commands.CheckFailure):
    """Raised when the invoker is not an administrator."""


class GuildOnlyError(app_commands.CheckFailure):
    """Raised when a guild-only command is invoked from a DM."""


class NotOwnerError(app_commands.CheckFailure):
    """Raised when a command reserved for the app owner is invoked by anyone else."""


def _member(interaction: discord.Interaction) -> discord.Member:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        raise GuildOnlyError("This command can only be used inside the server.")
    return interaction.user


def _has_role(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def is_admin_member(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator
        or member.id == member.guild.owner_id
        or _has_role(member, CONFIG.admin_role_id)
    )


def is_staff_member(member: discord.Member) -> bool:
    return is_admin_member(member) or _has_role(member, CONFIG.moderator_role_id)


def is_staff():
    """Allow moderators, administrators, and the guild owner."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = _member(interaction)
        if not is_staff_member(member):
            raise NotStaffError("This command is restricted to moderators.")
        return True

    return app_commands.check(predicate)


def is_admin():
    """Allow administrators and the guild owner only."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = _member(interaction)
        if not is_admin_member(member):
            raise NotAdminError("This command is restricted to administrators.")
        return True

    return app_commands.check(predicate)


# Group objects take a Permissions instance; the decorator below takes flags.
STAFF_DEFAULT_PERMISSIONS = discord.Permissions(manage_messages=True)
ADMIN_DEFAULT_PERMISSIONS = discord.Permissions(administrator=True)


def staff_only():
    """Grey the command out for non-moderators, and enforce it server-side.

    default_permissions alone is only a client-side hint: Discord still lets a
    user with the raw permission bit invoke the command. Pairing it with the
    is_staff check means role membership is what actually decides.
    """

    def decorator(func):
        func = is_staff()(func)
        return app_commands.default_permissions(manage_messages=True)(func)

    return decorator


def admin_only():
    """As staff_only, restricted to administrators."""

    def decorator(func):
        func = is_admin()(func)
        return app_commands.default_permissions(administrator=True)(func)

    return decorator


def owner_only():
    """Restrict a command to the single configured owner account.

    Deliberately not tied to guild permissions: a server administrator is not
    the app owner, and this gates bulk data export. default_permissions is set
    to no permissions so the command is hidden from everyone in the client, but
    the identity check below is what actually enforces it, because Discord lets
    guild administrators invoke commands regardless of that hint.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != CONFIG.owner_user_id:
            raise NotOwnerError("This command is restricted to the bot owner.")
        return True

    def decorator(func):
        func = app_commands.check(predicate)(func)
        return app_commands.default_permissions()(func)

    return decorator
