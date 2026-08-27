"""The /debate command: post a reference diagram into the channel.

The catalog is now reconciled against the images folder on every use. Previously
the label map was the only source of truth and the filename index was built once
at cog load, so 9 of the 20 images on disk were unreachable and adding a new one
required restarting the bot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands, ui
from discord.ext import commands

from core.config import CONFIG, EST
from core.errors import respond_error

logger = logging.getLogger(__name__)

_VIEW_TIMEOUT_SECONDS = 180
_OPTIONS_PER_SELECT = 25
_MAX_SELECTS = 5
_ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# Friendly labels for the files that have them. Anything else in the folder is
# offered under its filename stem rather than being hidden.
_DEFAULT_LABELS = {
    "VennDiagramSubmission.png": "Venn Diagram of Submission",
    "PrayerTimesDiagram.png": "Prayer Times",
    "10,13,15Hadith.jpg": "10, 13, 15 Hadith",
    "10 13 15 - Isnads.png": "10, 13, 15 Isnads",
    "Hafs vs Warsh - 8 differences.jpg": "Hafs vs Warsh (8 Differences)",
    "haziq -warsh v hafs.jpg": "Haziq: Warsh vs Hafs",
    "miskeen vs masakin.jpg": "Miskeen vs Masakin",
    "Extra Hadith Dietary Prohibitions.png": "Extra Hadith Dietary Prohibitions",
    "Prophet's Final Sermon(s).png": "Prophet's Final Sermon",
    "Muslim to Submitter.jpg": "Muslim to Submitter",
    "The Quran is Perfect, Fully-Detailed, etc.jpg": "Quran is Perfect / Fully Detailed",
    "Rashad Life Miracles.jpg": "Rashad Life Miracles",
}


@dataclass(frozen=True, slots=True)
class Diagram:
    label: str
    path: Path


class DiagramCatalog:
    """Reconciles the label overrides in JSON with what is actually on disk."""

    def __init__(self, folder: str, map_file: str) -> None:
        self._folder = Path(folder)
        self._map_file = Path(map_file)

    def load_label_overrides(self) -> dict[str, str]:
        """Read filename -> label overrides, tolerating the older label -> filename form."""
        if not self._map_file.exists():
            return dict(_DEFAULT_LABELS)

        try:
            raw = json.loads(self._map_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read %s, using defaults: %s", self._map_file, exc)
            return dict(_DEFAULT_LABELS)

        if not isinstance(raw, dict):
            return dict(_DEFAULT_LABELS)

        overrides = dict(_DEFAULT_LABELS)
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            # Legacy format was {label: filename}; current is {filename: label}.
            if Path(value).suffix.lower() in _ALLOWED_SUFFIXES:
                overrides[value] = key
            else:
                overrides[key] = value
        return overrides

    def scan(self) -> list[Diagram]:
        """Every usable image in the folder, labelled and sorted."""
        if not self._folder.is_dir():
            logger.error("Debate image folder not found: %s", self._folder)
            return []

        overrides = self.load_label_overrides()
        diagrams: list[Diagram] = []
        for path in sorted(self._folder.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _ALLOWED_SUFFIXES:
                logger.debug("Skipping non-image file %s", path.name)
                continue
            # A zero-byte file would fail the upload with an opaque error.
            try:
                if path.stat().st_size == 0:
                    logger.warning("Skipping empty image file %s", path.name)
                    continue
            except OSError as exc:
                logger.warning("Cannot stat %s: %s", path.name, exc)
                continue
            diagrams.append(Diagram(label=overrides.get(path.name, path.stem), path=path))

        diagrams.sort(key=lambda item: item.label.lower())
        capacity = _OPTIONS_PER_SELECT * _MAX_SELECTS
        if len(diagrams) > capacity:
            logger.warning(
                "%d diagrams found but only %d can be offered; trimming the rest",
                len(diagrams),
                capacity,
            )
        return diagrams[:capacity]


class DiagramSelect(ui.Select["DebateView"]):
    def __init__(self, diagrams: list[Diagram], *, page: int, pages: int) -> None:
        self._by_value = {str(index): diagram for index, diagram in enumerate(diagrams)}
        placeholder = "Choose a diagram..."
        if pages > 1:
            first = diagrams[0].label[:12]
            last = diagrams[-1].label[:12]
            placeholder = f"Diagrams {page}/{pages} ({first}…{last})"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=diagram.label[:100], value=value)
                for value, diagram in self._by_value.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        diagram = self._by_value.get(self.values[0])
        if diagram is None:
            await respond_error(interaction, "That selection is no longer available.")
            return

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await respond_error(interaction, "I cannot post a diagram here.")
            return

        if not diagram.path.is_file():
            logger.error("Diagram file disappeared: %s", diagram.path)
            await respond_error(interaction, f"`{diagram.path.name}` is missing on the server.")
            return

        embed = discord.Embed(
            title=f"📊 {diagram.label}",
            description="Visual reference",
            color=discord.Color.blue(),
            timestamp=datetime.now(EST),
        )
        embed.set_footer(
            text=f"Requested by {interaction.user.name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_image(url=f"attachment://{_safe_attachment_name(diagram.path)}")

        await interaction.response.defer(ephemeral=True)
        try:
            with diagram.path.open("rb") as handle:
                await channel.send(
                    embed=embed,
                    file=discord.File(handle, filename=_safe_attachment_name(diagram.path)),
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I do not have permission to upload files here.", ephemeral=True
            )
            return
        except (OSError, discord.HTTPException) as exc:
            logger.error("Failed to send diagram %s: %s", diagram.path.name, exc)
            await interaction.followup.send("❌ Could not upload that diagram.", ephemeral=True)
            return

        await interaction.followup.send("✅ Diagram sent to the channel.", ephemeral=True)


class DebateView(ui.View):
    def __init__(self, diagrams: list[Diagram]) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT_SECONDS)
        pages = max(1, (len(diagrams) + _OPTIONS_PER_SELECT - 1) // _OPTIONS_PER_SELECT)
        for index in range(pages):
            chunk = diagrams[index * _OPTIONS_PER_SELECT : (index + 1) * _OPTIONS_PER_SELECT]
            if chunk:
                self.add_item(DiagramSelect(chunk, page=index + 1, pages=pages))


class Debate(commands.Cog):
    """Reference diagrams for debates."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.catalog = DiagramCatalog(CONFIG.image_folder, CONFIG.debate_map_file)

    async def cog_load(self) -> None:
        count = len(self.catalog.scan())
        logger.info("Debate catalog contains %d diagram(s)", count)

    @app_commands.command(name="debate", description="Post a reference diagram in this channel.")
    @app_commands.guild_only()
    # Posts publicly, so it is rate limited rather than left wide open.
    @app_commands.checks.cooldown(3, 60.0)
    async def debate(self, interaction: discord.Interaction) -> None:
        # Rescanned per invocation so newly added images work without a restart.
        diagrams = self.catalog.scan()
        if not diagrams:
            await respond_error(interaction, "No diagrams are available.")
            return

        await interaction.response.send_message(
            f"Select a diagram to post ({len(diagrams)} available):",
            view=DebateView(diagrams),
            ephemeral=True,
        )


def _safe_attachment_name(path: Path) -> str:
    """Discord rejects some characters in filenames; keep the extension intact."""
    stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in path.stem)
    return f"{stem or 'diagram'}{path.suffix.lower()}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Debate(bot))
