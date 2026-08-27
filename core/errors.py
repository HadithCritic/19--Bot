"""Narrow exception types and one shared interaction responder.

The old code wrapped nearly every command body in `except Exception` and logged
it, which hid schema drift and None-dereferences for months. The pattern here is
to raise UserFacingError for expected problems, let unexpected ones reach the
tree error handler, and use respond() so a response is never sent twice.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


class BotError(Exception):
    """Base class for this bot's own errors."""


class UserFacingError(BotError):
    """An expected failure whose message is safe to show the invoker."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(BotError):
    """A guild object named in config.py is missing or inaccessible."""


async def respond(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = True,
    view: discord.ui.View | None = None,
) -> None:
    """Reply to an interaction whether or not it has already been responded to."""
    kwargs: dict[str, object] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)  # type: ignore[arg-type]
        else:
            await interaction.response.send_message(**kwargs)  # type: ignore[arg-type]
    except discord.HTTPException as exc:
        logger.warning("Could not respond to interaction %s: %s", interaction.command, exc)


async def respond_error(interaction: discord.Interaction, message: str) -> None:
    """Send an error message to the invoker, always ephemeral."""
    await respond(interaction, f"❌ {message}", ephemeral=True)
