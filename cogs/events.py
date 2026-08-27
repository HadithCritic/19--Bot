"""Custom live events: a modal to create them, plus listing and deactivation.

This cog was entirely broken in production. Its INSERT and SELECT statements
named columns (user_id, username, image_url, is_active) that the live table did
not have, so every /addevent image capture and every /listevents call failed
with a hard SQLite error that the blanket except swallowed. Migration 4 aligns
the schema; the code below matches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from core.checks import staff_only
from core.errors import respond, respond_error

logger = logging.getLogger(__name__)

_VIEW_TIMEOUT_SECONDS = 300
# How long we watch for the organiser's image upload before giving up.
_PENDING_TTL = timedelta(minutes=10)
_MAX_PENDING_EVENTS = 25
_SELECT_OPTION_LIMIT = 25


@dataclass(slots=True)
class PendingEvent:
    """An event awaiting its optional image upload."""

    title: str
    message: str
    voice_channel_id: int
    voice_channel_name: str
    guild_id: int
    channel_id: int
    created_at: datetime

    @property
    def expires_at(self) -> datetime:
        return self.created_at + _PENDING_TTL

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class PendingEventStore:
    """Bounded, self-expiring store for in-flight event drafts.

    The previous dict grew without limit, survived nothing across restarts, and
    kept watching an author's messages in every channel forever.
    """

    def __init__(self) -> None:
        self._items: dict[int, PendingEvent] = {}

    def put(self, user_id: int, event: PendingEvent) -> None:
        self.prune(event.created_at)
        if len(self._items) >= _MAX_PENDING_EVENTS and user_id not in self._items:
            oldest = min(self._items, key=lambda key: self._items[key].created_at)
            del self._items[oldest]
            logger.warning("Pending event store full, evicted draft for %s", oldest)
        self._items[user_id] = event

    def get(self, user_id: int, now: datetime) -> PendingEvent | None:
        event = self._items.get(user_id)
        if event is None:
            return None
        if event.is_expired(now):
            del self._items[user_id]
            return None
        return event

    def discard(self, user_id: int) -> None:
        self._items.pop(user_id, None)

    def prune(self, now: datetime) -> int:
        expired = [uid for uid, event in self._items.items() if event.is_expired(now)]
        for user_id in expired:
            del self._items[user_id]
        return len(expired)

    def __len__(self) -> int:
        return len(self._items)


def voice_channel_options(guild: discord.Guild) -> list[discord.SelectOption]:
    """Build the channel dropdown from the guild's real channels.

    Three channel IDs were hardcoded here previously, and they were consecutive
    integers, which no real set of Discord snowflakes ever is: at most one of
    them could have resolved.
    """
    channels: list[discord.abc.GuildChannel] = [
        *sorted(guild.voice_channels, key=lambda c: c.position),
        *sorted(guild.stage_channels, key=lambda c: c.position),
    ]
    return [
        discord.SelectOption(
            label=channel.name[:100],
            value=str(channel.id),
            emoji="🎤" if isinstance(channel, discord.StageChannel) else "🔊",
        )
        for channel in channels[:_SELECT_OPTION_LIMIT]
    ]


class AddLiveEventModal(ui.Modal, title="Add Custom Live Event"):
    event_title: ui.TextInput = ui.TextInput(
        label="Event Title", placeholder="Enter the title of your event", max_length=100
    )
    event_message: ui.TextInput = ui.TextInput(
        label="Event Message",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your event...",
        max_length=1000,
    )

    def __init__(self, cog: Events) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await respond_error(interaction, "This only works inside the server.")
            return

        options = voice_channel_options(guild)
        if not options:
            await respond_error(interaction, "This server has no voice or stage channels.")
            return

        embed = discord.Embed(
            title="📢 Choose a Voice Channel",
            description=(
                f"**Event:** {self.event_title.value}\n"
                f"**Message:** {self.event_message.value}\n\n"
                "Select where this event will be held."
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        view = VoiceChannelSelectionView(
            cog=self.cog,
            title=self.event_title.value,
            message=self.event_message.value,
            options=options,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Add event modal failed", exc_info=error)
        await respond_error(interaction, "Something went wrong creating that event.")


class VoiceChannelSelectionView(ui.View):
    def __init__(
        self,
        *,
        cog: Events,
        title: str,
        message: str,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT_SECONDS)
        self.cog = cog
        self.event_title = title
        self.event_message = message
        self.add_item(VoiceChannelSelect(options))


class VoiceChannelSelect(ui.Select["VoiceChannelSelectionView"]):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="Select a voice channel...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None or interaction.guild is None:
            await respond_error(interaction, "This selection has expired.")
            return

        channel_id = int(self.values[0])
        channel = interaction.guild.get_channel(channel_id)
        channel_name = channel.name if channel else "Unknown channel"

        # The draft is only registered once a channel is actually chosen.
        view.cog.pending.put(
            interaction.user.id,
            PendingEvent(
                title=view.event_title,
                message=view.event_message,
                voice_channel_id=channel_id,
                voice_channel_name=channel_name,
                guild_id=interaction.guild.id,
                channel_id=interaction.channel_id or 0,
                created_at=discord.utils.utcnow(),
            ),
        )

        embed = discord.Embed(
            title="🖼️ Optional: Add an Image",
            description=(
                f"Selected channel: **{channel_name}**\n\n"
                "Post an image in this channel within the next 10 minutes to attach it, "
                "or press **Save without image** to finish now."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=FinishEventView(view.cog))


class FinishEventView(ui.View):
    def __init__(self, cog: Events) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT_SECONDS)
        self.cog = cog

    @ui.button(label="Save without image", style=discord.ButtonStyle.primary, emoji="💾")
    async def save_now(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        pending = self.cog.pending.get(interaction.user.id, discord.utils.utcnow())
        if pending is None:
            await respond_error(interaction, "That draft has expired, please start again.")
            return

        event_id = await self.cog.save_event(interaction.user, pending, image_url=None)
        self.cog.pending.discard(interaction.user.id)
        await interaction.response.edit_message(
            content=f"✅ Event **{pending.title}** saved as ID `{event_id}`.",
            embed=None,
            view=None,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        self.cog.pending.discard(interaction.user.id)
        await interaction.response.edit_message(
            content="Event creation cancelled.", embed=None, view=None
        )


class Events(commands.Cog):
    """Creation and management of custom live events."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]
        self.pending = PendingEventStore()

    async def cog_load(self) -> None:
        self.prune_pending.start()

    async def cog_unload(self) -> None:
        self.prune_pending.cancel()

    # --- Commands ---

    @app_commands.command(name="addevent", description="Create a custom live event.")
    @app_commands.guild_only()
    @staff_only()
    async def addevent(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddLiveEventModal(self))

    @app_commands.command(name="listevents", description="List all active custom live events.")
    @app_commands.guild_only()
    @staff_only()
    async def listevents(self, interaction: discord.Interaction) -> None:
        rows = await self.db.fetch_all(
            """SELECT id, title, created_by, created_by_name, voice_channel_name, created_at
               FROM custom_live_events
               WHERE is_active = 1 AND (guild_id IS NULL OR guild_id = ?)
               ORDER BY created_at DESC
               LIMIT 25""",
            (interaction.guild_id,),
        )
        if not rows:
            await respond(interaction, "ℹ️ No active events found.")
            return

        embed = discord.Embed(
            title="📅 Active Custom Live Events",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        for row in rows:
            host = row["created_by_name"] or f"<@{row['created_by']}>"
            embed.add_field(
                name=f"ID {row['id']} — {row['title']}",
                value=f"Host: {host}\nChannel: {row['voice_channel_name'] or 'unknown'}",
                inline=False,
            )
        embed.set_footer(text=f"{len(rows)} active")
        await respond(interaction, embed=embed)

    @app_commands.command(name="deleteevent", description="Deactivate a custom live event by ID.")
    @app_commands.describe(event_id="The ID shown by /listevents.")
    @app_commands.guild_only()
    @staff_only()
    async def deleteevent(self, interaction: discord.Interaction, event_id: int) -> None:
        # Soft delete, so the is_active column that /listevents filters on is
        # actually meaningful and the record survives for auditing.
        affected = await self.db.execute(
            "UPDATE custom_live_events SET is_active = 0 WHERE id = ? AND is_active = 1",
            (event_id,),
        )
        if affected == 0:
            await respond_error(interaction, f"No active event with ID `{event_id}`.")
            return

        logger.info("%s deactivated event %s", interaction.user.id, event_id)
        await respond(interaction, f"✅ Event `{event_id}` deactivated.", ephemeral=False)

    # --- Persistence ---

    async def save_event(
        self,
        author: discord.User | discord.Member,
        pending: PendingEvent,
        *,
        image_url: str | None,
    ) -> int:
        await self.db.execute(
            """INSERT INTO custom_live_events
                   (guild_id, created_by, created_by_name, title, message,
                    voice_channel_id, voice_channel_name, image_url, created_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                pending.guild_id,
                author.id,
                author.name,
                pending.title,
                pending.message,
                pending.voice_channel_id,
                pending.voice_channel_name,
                image_url,
                discord.utils.utcnow().isoformat(),
            ),
        )
        row = await self.db.fetch_one("SELECT last_insert_rowid() AS id")
        event_id = int(row["id"]) if row else 0
        logger.info("Saved event %s (%s) for %s", event_id, pending.title, author.id)
        return event_id

    # --- Listeners ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Attach an image the organiser posts in the same channel as the draft."""
        if message.author.bot or message.guild is None or not message.attachments:
            return

        now = discord.utils.utcnow()
        pending = self.pending.get(message.author.id, now)
        if pending is None:
            return
        # Scoped to the channel where the flow started, so an unrelated image
        # posted elsewhere is not swallowed into the event.
        if message.channel.id != pending.channel_id:
            return

        attachment = next(
            (
                item
                for item in message.attachments
                if item.content_type and item.content_type.startswith("image/")
            ),
            None,
        )
        if attachment is None:
            return

        event_id = await self.save_event(message.author, pending, image_url=attachment.url)
        self.pending.discard(message.author.id)

        preview = discord.Embed(
            title=pending.title,
            description=(f"{pending.message}\n\n📍 **Location:** <#{pending.voice_channel_id}>"),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        preview.set_image(url=attachment.url)
        preview.set_footer(text=f"Hosted by {message.author.display_name} • ID {event_id}")

        try:
            await message.channel.send(
                f"✅ Event **{pending.title}** saved as ID `{event_id}`.", embed=preview
            )
        except discord.HTTPException as exc:
            logger.error("Saved event %s but could not post the preview: %s", event_id, exc)

    # --- Tasks ---

    @tasks.loop(minutes=5)
    async def prune_pending(self) -> None:
        removed = self.pending.prune(discord.utils.utcnow())
        if removed:
            logger.debug("Pruned %d expired event draft(s)", removed)

    @prune_pending.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    @prune_pending.error
    async def _task_error(self, error: BaseException) -> None:
        logger.exception("Event pruning task failed", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
