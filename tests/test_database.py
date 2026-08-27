import sqlite3

import pytest

from core.database import DEFAULT_BOT_WHITELIST, MIGRATIONS, Database, DatabaseError

pytestmark = pytest.mark.integration


async def test_connect_applies_every_migration(db):
    assert await db.schema_version() == MIGRATIONS[-1][0]


async def test_wal_is_enabled(db):
    row = await db.fetch_one("PRAGMA journal_mode")
    assert row[0].lower() == "wal"


async def test_migrate_is_idempotent(db):
    before = await db.schema_version()
    await db.migrate()
    await db.migrate()
    assert await db.schema_version() == before
    rows = await db.fetch_all("SELECT version FROM schema_migrations")
    assert len(rows) == len({row["version"] for row in rows})


async def test_whitelist_is_seeded_once(db):
    rows = await db.fetch_all("SELECT bot_id FROM whitelist")
    assert {row["bot_id"] for row in rows} == set(DEFAULT_BOT_WHITELIST)


async def test_removed_default_bot_stays_removed_after_remigration(db):
    """The old code re-seeded defaults on every startup, undoing removals."""
    target = DEFAULT_BOT_WHITELIST[0]
    await db.execute("DELETE FROM whitelist WHERE bot_id = ?", (target,))
    await db.migrate()
    assert not await db.exists("SELECT 1 FROM whitelist WHERE bot_id = ?", (target,))


async def test_expected_tables_exist(db):
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] for row in rows}
    assert {
        "whitelist",
        "after_ban_users",
        "perpetual_timeouts",
        "new_member_roles",
        "suspicious_accounts",
        "custom_live_events",
        "blog_state",
        "schema_migrations",
    } <= names


async def test_suspicious_accounts_rejects_duplicate_users(db):
    row = ("guild", "name", "display", "created", "joined", "flagged", 1, 1)
    await db.execute(
        """INSERT INTO suspicious_accounts
               (user_id, guild_id, username, display_name, account_created_at,
                joined_at, flagged_at, unverified_role_assigned, staff_notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (1, *row),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            """INSERT INTO suspicious_accounts
                   (user_id, guild_id, username, display_name, account_created_at,
                    joined_at, flagged_at, unverified_role_assigned, staff_notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (1, *row),
        )


async def test_events_table_has_the_columns_the_cog_queries(db):
    rows = await db.fetch_all("PRAGMA table_info(custom_live_events)")
    columns = {row["name"] for row in rows}
    # These four were missing in production, so every /listevents call failed.
    assert {"created_by", "created_by_name", "image_url", "is_active"} <= columns


async def test_legacy_database_migrates_and_deduplicates(tmp_path):
    """Simulate the real deployment: pre-existing tables, no version marker."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE suspicious_accounts (
            id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT,
            display_name TEXT, account_created_at TEXT, joined_at TEXT,
            flagged_at TEXT, guild_id INTEGER, is_same_day_join BOOLEAN,
            has_activity BOOLEAN, unverified_role_assigned BOOLEAN,
            staff_notified BOOLEAN
        );
        CREATE TABLE custom_live_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, message TEXT,
            voice_channel_id INTEGER, voice_channel_name TEXT,
            image_filename TEXT, created_by INTEGER, created_at TEXT,
            guild_id INTEGER
        );
        CREATE TABLE whitelist (bot_id INTEGER PRIMARY KEY);
        """
    )
    # The same user flagged three times, which the old schema allowed.
    for i in range(3):
        conn.execute(
            "INSERT INTO suspicious_accounts (user_id, username, flagged_at, guild_id) "
            "VALUES (?, ?, ?, ?)",
            (42, f"name{i}", f"2025-01-0{i + 1}", 7),
        )
    conn.execute(
        "INSERT INTO custom_live_events (title, message, voice_channel_id, created_by, created_at) "
        "VALUES ('t', 'm', 5, 9, '2025-01-01')"
    )
    conn.commit()
    conn.close()

    database = Database(str(path))
    await database.connect()
    try:
        rows = await database.fetch_all("SELECT user_id, username FROM suspicious_accounts")
        assert len(rows) == 1
        assert rows[0]["username"] == "name2"  # the most recent row survives

        events = await database.fetch_all("SELECT title, is_active FROM custom_live_events")
        assert len(events) == 1
        assert events[0]["is_active"] == 1
    finally:
        await database.close()


async def test_execute_reports_affected_rows(db):
    await db.execute("INSERT INTO whitelist (bot_id) VALUES (?)", (12345,))
    assert await db.execute("DELETE FROM whitelist WHERE bot_id = ?", (12345,)) == 1
    assert await db.execute("DELETE FROM whitelist WHERE bot_id = ?", (12345,)) == 0


async def test_use_before_connect_is_an_explicit_error(tmp_path):
    with pytest.raises(DatabaseError, match="connect"):
        _ = Database(str(tmp_path / "x.db")).connection
