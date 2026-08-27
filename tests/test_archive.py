"""Tests for the archive command's pure logic and owner gating."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.archive import (
    ArchiveStats,
    _format_duration,
    safe_name,
    serialise_message,
)
from core.config import CONFIG

pytestmark = pytest.mark.unit


# --- Path safety ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("general", "general"),
        ("mod-chat", "mod-chat"),
        ("💬-chat", "chat"),
        # Leading dot-segments are stripped, so traversal cannot survive.
        ("../../etc/passwd", "etc_passwd"),
        ("..", "unnamed"),
        ("a/b\\c", "a_b_c"),
        ("con:aux", "con_aux"),
        ("", "unnamed"),
        ("...", "unnamed"),
        ("///", "unnamed"),
    ],
)
def test_safe_name_produces_one_safe_segment(raw, expected):
    assert safe_name(raw) == expected


def test_safe_name_never_escapes_its_directory():
    for hostile in ("../secret", "..\\secret", "/etc/passwd", "C:\\Windows"):
        result = safe_name(hostile)
        assert "/" not in result
        assert "\\" not in result
        assert not Path(result).is_absolute()
        # The joined path must stay inside the base directory.
        base = Path("archives").resolve()
        assert (
            base in (base / result).resolve().parents or (base / result).resolve() == base / result
        )


def test_safe_name_is_length_capped():
    assert len(safe_name("x" * 500)) <= 80


# --- Message serialisation ---


def fake_message(*, content="hello", attachments=(), embeds=(), edited=False):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=111,
        channel=SimpleNamespace(id=222),
        type="MessageType.default",
        created_at=now,
        edited_at=now if edited else None,
        author=SimpleNamespace(id=333, name="user", display_name="User", bot=False),
        content=content,
        clean_content=content,
        pinned=False,
        tts=False,
        reference=None,
        attachments=list(attachments),
        embeds=list(embeds),
        stickers=[],
        reactions=[],
        mentions=[],
        role_mentions=[],
        jump_url="https://discord.com/channels/1/2/3",
    )


def fake_attachment(**overrides):
    defaults = {
        "id": 999,
        "filename": "image.png",
        "content_type": "image/png",
        "size": 1024,
        "url": "https://cdn.discordapp.com/attachments/1/2/image.png",
        "description": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(is_spoiler=lambda: False, **defaults)


def test_serialise_captures_the_core_fields():
    record = serialise_message(fake_message())  # type: ignore[arg-type]
    assert record["id"] == 111
    assert record["content"] == "hello"
    assert record["author"]["id"] == 333
    assert record["edited_at"] is None
    assert record["attachments"] == []


def test_serialise_records_edits():
    record = serialise_message(fake_message(edited=True))  # type: ignore[arg-type]
    assert record["edited_at"] is not None


def test_serialise_records_attachment_metadata():
    record = serialise_message(  # type: ignore[arg-type]
        fake_message(attachments=[fake_attachment(filename="cat.jpg", size=4096)])
    )
    assert len(record["attachments"]) == 1
    assert record["attachments"][0]["filename"] == "cat.jpg"
    assert record["attachments"][0]["size"] == 4096


def test_serialise_output_is_json_encodable():
    import json

    record = serialise_message(fake_message(attachments=[fake_attachment()]))  # type: ignore[arg-type]
    assert json.loads(json.dumps(record, ensure_ascii=False))["id"] == 111


def test_serialise_preserves_unicode_content():
    import json

    record = serialise_message(fake_message(content="سلام 🕊️"))  # type: ignore[arg-type]
    assert json.loads(json.dumps(record, ensure_ascii=False))["content"] == "سلام 🕊️"


# --- Stats ---


def test_error_list_is_capped():
    stats = ArchiveStats()
    for i in range(100):
        stats.note_error(f"error {i}")
    assert len(stats.errors) == 10


def test_stats_start_empty():
    stats = ArchiveStats()
    assert stats.messages == 0
    assert stats.last_message_id is None


# --- Duration formatting ---


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (90, "1m 30s"),
        (3600, "1h 0m"),
        (7325, "2h 2m"),
    ],
)
def test_duration_formatting(seconds, expected):
    assert _format_duration(seconds) == expected


# --- Owner gating ---


def _predicate_of(decorator, func):
    """Pull the check predicate that app_commands.check attached to func."""
    decorated = decorator(func)
    return decorated.__discord_app_commands_checks__[0]


async def test_owner_check_allows_only_the_configured_owner():
    from core.checks import NotOwnerError, owner_only

    def command_stub():
        pass

    predicate = _predicate_of(owner_only(), command_stub)

    owner = SimpleNamespace(user=SimpleNamespace(id=CONFIG.owner_user_id))
    assert await predicate(owner) is True

    for impostor_id in (0, 1, 999_999_999, CONFIG.owner_user_id + 1):
        with pytest.raises(NotOwnerError):
            await predicate(SimpleNamespace(user=SimpleNamespace(id=impostor_id)))


async def test_a_guild_administrator_is_not_the_owner():
    """default_permissions cannot gate this: admins bypass that hint."""
    from core.checks import NotOwnerError, owner_only

    def command_stub():
        pass

    predicate = _predicate_of(owner_only(), command_stub)

    admin = SimpleNamespace(
        user=SimpleNamespace(id=555, guild_permissions=SimpleNamespace(administrator=True))
    )
    with pytest.raises(NotOwnerError):
        await predicate(admin)


# --- Folder layout ---


def test_single_channel_target_folder():
    from cogs.archive import ChannelArchiver
    import discord

    dest = Path("archives/123/general-456")
    archiver = ChannelArchiver(
        destination=dest,
        session=None,  # type: ignore[arg-type]
        include_attachments=False,
        max_attachment_bytes=100,
        stats=ArchiveStats(),
        is_server_archive=False,
    )

    channel = SimpleNamespace(name="general", id=456)
    assert archiver._target_folder(channel) == dest / "general"  # type: ignore[arg-type]


def test_server_archive_target_folders():
    from cogs.archive import ChannelArchiver
    import discord

    dest = Path("archives/123/server-myserver-123")
    archiver = ChannelArchiver(
        destination=dest,
        session=None,  # type: ignore[arg-type]
        include_attachments=False,
        max_attachment_bytes=100,
        stats=ArchiveStats(),
        is_server_archive=True,
    )

    cat = SimpleNamespace(name="General Category")
    channel = SimpleNamespace(name="welcome", id=789, category=cat)
    assert archiver._target_folder(channel) == dest / "General_Category" / "welcome-789"  # type: ignore[arg-type]

