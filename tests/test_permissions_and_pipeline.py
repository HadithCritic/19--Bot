"""Tests for permission resolution and the member-join ordering.

These are the two behaviours where a regression is most costly: a broken check
exposes moderator tooling, and a broken pipeline order lets a banned user
through or double-processes a join.
"""

from types import SimpleNamespace

import pytest

from cogs.debate import _safe_attachment_name
from cogs.events import voice_channel_options
from cogs.membership import MemberGateway
from cogs.moderation import _format_date
from cogs.security import _parse_snowflake
from core.checks import is_admin_member, is_staff_member
from core.config import CONFIG

pytestmark = pytest.mark.unit


# --- Permission resolution (core/checks.py) ---


def fake_member(*, role_ids=(), administrator=False, owner=False, user_id=100):
    guild = SimpleNamespace(owner_id=user_id if owner else 999_999)
    return SimpleNamespace(
        id=user_id,
        guild=guild,
        guild_permissions=SimpleNamespace(administrator=administrator),
        roles=[SimpleNamespace(id=rid) for rid in role_ids],
    )


def test_plain_member_is_neither_staff_nor_admin():
    member = fake_member()
    assert not is_staff_member(member)  # type: ignore[arg-type]
    assert not is_admin_member(member)  # type: ignore[arg-type]


def test_moderator_role_grants_staff_but_not_admin():
    member = fake_member(role_ids=(CONFIG.moderator_role_id,))
    assert is_staff_member(member)  # type: ignore[arg-type]
    assert not is_admin_member(member)  # type: ignore[arg-type]


def test_admin_role_grants_both():
    member = fake_member(role_ids=(CONFIG.admin_role_id,))
    assert is_admin_member(member)  # type: ignore[arg-type]
    assert is_staff_member(member)  # type: ignore[arg-type]


def test_administrator_permission_grants_both():
    member = fake_member(administrator=True)
    assert is_admin_member(member)  # type: ignore[arg-type]
    assert is_staff_member(member)  # type: ignore[arg-type]


def test_guild_owner_grants_both():
    member = fake_member(owner=True)
    assert is_admin_member(member)  # type: ignore[arg-type]
    assert is_staff_member(member)  # type: ignore[arg-type]


def test_an_unrelated_role_grants_nothing():
    member = fake_member(role_ids=(12345,))
    assert not is_staff_member(member)  # type: ignore[arg-type]


# --- Join pipeline ordering (cogs/membership.py) ---


class FakeSecurity:
    def __init__(self, *, kicks=False, suspicious=False):
        self._kicks = kicks
        self._suspicious = suspicious
        self.calls: list[str] = []

    async def check_unauthorized_bot(self, member):
        self.calls.append("check_unauthorized_bot")
        return self._kicks

    async def flag_suspicious_account(self, member):
        self.calls.append("flag_suspicious_account")
        return self._suspicious


class FakeModeration:
    def __init__(self, *, bans=False):
        self._bans = bans
        self.calls: list[str] = []

    async def enforce_after_ban(self, member):
        self.calls.append("enforce_after_ban")
        return self._bans

    async def assign_new_member_role(self, member):
        self.calls.append("assign_new_member_role")
        return True

    async def send_welcome_flow(self, member):
        self.calls.append("send_welcome_flow")


class FakeBot:
    def __init__(self, cogs):
        self._cogs = cogs

    def get_cog(self, name):
        return self._cogs.get(name)


def make_gateway(security=None, moderation=None):
    cogs = {}
    if security is not None:
        cogs["Security"] = security
    if moderation is not None:
        cogs["Moderation"] = moderation
    return MemberGateway(FakeBot(cogs))  # type: ignore[arg-type]


def joining_member(*, guild_id=None, bot=False):
    return SimpleNamespace(
        id=7,
        bot=bot,
        guild=SimpleNamespace(id=guild_id if guild_id is not None else CONFIG.guild_id),
    )


async def test_normal_join_runs_every_step_in_order():
    security, moderation = FakeSecurity(), FakeModeration()
    await make_gateway(security, moderation).on_member_join(joining_member())  # type: ignore[arg-type]
    assert security.calls == ["check_unauthorized_bot", "flag_suspicious_account"]
    assert moderation.calls == [
        "enforce_after_ban",
        "assign_new_member_role",
        "send_welcome_flow",
    ]


async def test_kicked_bot_stops_the_pipeline():
    security = FakeSecurity(kicks=True)
    moderation = FakeModeration()
    await make_gateway(security, moderation).on_member_join(joining_member(bot=True))  # type: ignore[arg-type]
    assert security.calls == ["check_unauthorized_bot"]
    assert moderation.calls == []


async def test_whitelisted_bot_gets_no_roles_or_dms():
    security, moderation = FakeSecurity(), FakeModeration()
    await make_gateway(security, moderation).on_member_join(joining_member(bot=True))  # type: ignore[arg-type]
    assert moderation.calls == []


async def test_after_ban_stops_before_roles_and_dms():
    """Previously two racing listeners could ban and welcome the same user."""
    security = FakeSecurity()
    moderation = FakeModeration(bans=True)
    await make_gateway(security, moderation).on_member_join(joining_member())  # type: ignore[arg-type]
    assert moderation.calls == ["enforce_after_ban"]
    assert "flag_suspicious_account" not in security.calls


async def test_suspicious_account_still_receives_role_and_welcome():
    security = FakeSecurity(suspicious=True)
    moderation = FakeModeration()
    await make_gateway(security, moderation).on_member_join(joining_member())  # type: ignore[arg-type]
    assert "assign_new_member_role" in moderation.calls
    assert "send_welcome_flow" in moderation.calls


async def test_other_guilds_are_ignored():
    """The old Security listener had no guild guard."""
    security, moderation = FakeSecurity(), FakeModeration()
    await make_gateway(security, moderation).on_member_join(joining_member(guild_id=1))  # type: ignore[arg-type]
    assert security.calls == []
    assert moderation.calls == []


async def test_missing_cog_aborts_instead_of_crashing():
    security = FakeSecurity()
    await make_gateway(security, None).on_member_join(joining_member())  # type: ignore[arg-type]
    assert security.calls == []


# --- Small helpers ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("437618149505105920", 437618149505105920),
        ("  437618149505105920  ", 437618149505105920),
        ("<@437618149505105920>", 437618149505105920),
        ("<@!437618149505105920>", 437618149505105920),
        ("123", None),
        ("not-a-number", None),
        ("", None),
        ("-437618149505105920", None),
        ("999999999999999999999999", None),
    ],
)
def test_snowflake_parsing(raw, expected):
    assert _parse_snowflake(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-01-02T03:04:05+00:00", "2025-01-02"),
        ("garbage", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_date_formatting_never_raises(raw, expected):
    assert _format_date(raw) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("simple.png", "simple.png"),
        ("Hafs vs Warsh - 8 differences.jpg", "Hafs_vs_Warsh_-_8_differences.jpg"),
        ("10,13,15Hadith.jpg", "10_13_15Hadith.jpg"),
        ("Prophet's Final Sermon(s).PNG", "Prophet_s_Final_Sermon_s_.png"),
    ],
)
def test_attachment_names_are_sanitised(filename, expected):
    from pathlib import Path

    assert _safe_attachment_name(Path(filename)) == expected


def test_voice_channel_options_are_built_from_the_guild():
    """Three hardcoded, consecutive channel IDs used to be baked in here."""

    class FakeVoice:
        def __init__(self, name, position, channel_id):
            self.name = name
            self.position = position
            self.id = channel_id

    guild = SimpleNamespace(
        voice_channels=[FakeVoice("Second", 2, 20), FakeVoice("First", 1, 10)],
        stage_channels=[],
    )
    options = voice_channel_options(guild)  # type: ignore[arg-type]
    assert [o.label for o in options] == ["First", "Second"]
    assert [o.value for o in options] == ["10", "20"]


def test_voice_channel_options_are_capped_at_the_discord_limit():
    class FakeVoice:
        def __init__(self, i):
            self.name = f"VC{i}"
            self.position = i
            self.id = 1000 + i

    guild = SimpleNamespace(voice_channels=[FakeVoice(i) for i in range(40)], stage_channels=[])
    assert len(voice_channel_options(guild)) == 25  # type: ignore[arg-type]


def test_voice_channel_options_handles_an_empty_guild():
    guild = SimpleNamespace(voice_channels=[], stage_channels=[])
    assert voice_channel_options(guild) == []  # type: ignore[arg-type]
