"""A single shared aiosqlite connection with WAL and versioned migrations.

The previous design opened a fresh connection for every query (19 call sites)
with SQLite's default rollback journal, which deadlocks under concurrent writes
from the background tasks. One long-lived connection serialises all access
through aiosqlite's worker thread, so writes queue instead of colliding.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

Row = aiosqlite.Row


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened or migrated."""


# Bots trusted to join the guild. Seeded exactly once, by migration 2, so that
# removals via /botwhitelist remove are not silently undone on the next restart.
DEFAULT_BOT_WHITELIST = (
    978658099474890793,
    1267475883036377194,
    821050098557517885,
    735147814878969968,
    302050872383242240,
    272937604339466240,
    361033318273384449,
    1377858454168928287,
    1222548162741538938,
    1212469137897422888,
    437618149505105920,  # EasyPoll
)


# One migration statement: either bare SQL, or SQL paired with bound parameters.
Statement = str | tuple[str, Sequence[Any]]

# Each migration is (version, description, statements). Statements run inside one
# transaction per migration, so a failure leaves the version unrecorded.
MIGRATIONS: tuple[tuple[int, str, tuple[Statement, ...]], ...] = (
    (
        1,
        "baseline tables",
        (
            "CREATE TABLE IF NOT EXISTS whitelist (bot_id INTEGER PRIMARY KEY)",
            """CREATE TABLE IF NOT EXISTS after_ban_users (
                   user_id    INTEGER PRIMARY KEY,
                   reason     TEXT,
                   added_by   INTEGER,
                   added_time TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS perpetual_timeouts (
                   user_id         INTEGER PRIMARY KEY,
                   reason          TEXT,
                   added_by        INTEGER,
                   added_at        TEXT,
                   last_checked_at TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS new_member_roles (
                   user_id       INTEGER PRIMARY KEY,
                   guild_id      INTEGER,
                   assigned_time TEXT,
                   expires_time  TEXT
               )""",
        ),
    ),
    (
        2,
        "seed default bot whitelist once",
        tuple(
            ("INSERT OR IGNORE INTO whitelist (bot_id) VALUES (?)", (bot_id,))
            for bot_id in DEFAULT_BOT_WHITELIST
        ),
    ),
    (
        3,
        "rebuild suspicious_accounts with a unique user_id and drop dead columns",
        (
            # Created only if this is a fresh database; existing deployments
            # already have the legacy 12-column table.
            """CREATE TABLE IF NOT EXISTS suspicious_accounts (
                   user_id                  INTEGER,
                   username                 TEXT,
                   display_name             TEXT,
                   account_created_at       TEXT,
                   joined_at                TEXT,
                   flagged_at               TEXT,
                   guild_id                 INTEGER,
                   unverified_role_assigned INTEGER DEFAULT 0,
                   staff_notified           INTEGER DEFAULT 0
               )""",
            """CREATE TABLE suspicious_accounts_new (
                   user_id                  INTEGER PRIMARY KEY,
                   guild_id                 INTEGER,
                   username                 TEXT,
                   display_name             TEXT,
                   account_created_at       TEXT,
                   joined_at                TEXT,
                   flagged_at               TEXT,
                   unverified_role_assigned INTEGER NOT NULL DEFAULT 0,
                   staff_notified           INTEGER NOT NULL DEFAULT 0
               )""",
            # Keep the most recent flag per user. The old table had no unique
            # constraint, so rejoining users accumulated duplicate rows.
            """INSERT INTO suspicious_accounts_new
                   (user_id, guild_id, username, display_name,
                    account_created_at, joined_at, flagged_at,
                    unverified_role_assigned, staff_notified)
               SELECT user_id, guild_id, username, display_name,
                      account_created_at, joined_at, flagged_at,
                      COALESCE(unverified_role_assigned, 0),
                      COALESCE(staff_notified, 0)
               FROM suspicious_accounts
               WHERE user_id IS NOT NULL
                 AND rowid IN (
                     SELECT MAX(rowid) FROM suspicious_accounts GROUP BY user_id
                 )""",
            "DROP TABLE suspicious_accounts",
            "ALTER TABLE suspicious_accounts_new RENAME TO suspicious_accounts",
            "CREATE INDEX IF NOT EXISTS idx_suspicious_flagged_at ON suspicious_accounts (flagged_at)",
        ),
    ),
    (
        4,
        "rebuild custom_live_events to match the code that queries it",
        (
            """CREATE TABLE IF NOT EXISTS custom_live_events (
                   id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                   title              TEXT,
                   message            TEXT,
                   voice_channel_id   INTEGER,
                   voice_channel_name TEXT,
                   image_filename     TEXT,
                   created_by         INTEGER,
                   created_at         TEXT,
                   guild_id           INTEGER
               )""",
            """CREATE TABLE custom_live_events_new (
                   id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                   guild_id           INTEGER,
                   created_by         INTEGER NOT NULL,
                   created_by_name    TEXT,
                   title              TEXT NOT NULL,
                   message            TEXT NOT NULL,
                   voice_channel_id   INTEGER NOT NULL,
                   voice_channel_name TEXT,
                   image_url          TEXT,
                   created_at         TEXT NOT NULL,
                   is_active          INTEGER NOT NULL DEFAULT 1
               )""",
            """INSERT INTO custom_live_events_new
                   (id, guild_id, created_by, created_by_name, title, message,
                    voice_channel_id, voice_channel_name, image_url,
                    created_at, is_active)
               SELECT id, guild_id, COALESCE(created_by, 0), NULL,
                      COALESCE(title, ''), COALESCE(message, ''),
                      COALESCE(voice_channel_id, 0), voice_channel_name,
                      image_filename, COALESCE(created_at, ''), 1
               FROM custom_live_events""",
            "DROP TABLE custom_live_events",
            "ALTER TABLE custom_live_events_new RENAME TO custom_live_events",
            "CREATE INDEX IF NOT EXISTS idx_events_active ON custom_live_events (is_active)",
        ),
    ),
    (
        5,
        "move RSS feed state out of blog_data.json into the database",
        (
            """CREATE TABLE IF NOT EXISTS blog_state (
                   feed_id       TEXT PRIMARY KEY,
                   last_entry_id TEXT,
                   checked_at    TEXT
               )""",
        ),
    ),
    (
        6,
        "index the new-member expiry scan",
        ("CREATE INDEX IF NOT EXISTS idx_new_member_expires ON new_member_roles (expires_time)",),
    ),
    (
        7,
        "channel archive bookkeeping, so a run can resume where it stopped",
        (
            """CREATE TABLE IF NOT EXISTS archive_runs (
                   channel_id       INTEGER PRIMARY KEY,
                   guild_id         INTEGER,
                   channel_name     TEXT,
                   last_message_id  INTEGER,
                   message_count    INTEGER NOT NULL DEFAULT 0,
                   attachment_count INTEGER NOT NULL DEFAULT 0,
                   started_at       TEXT,
                   completed_at     TEXT,
                   status           TEXT NOT NULL DEFAULT 'running'
               )""",
        ),
    ),
    (
        8,
        "remember the last synced command set, to avoid needless re-syncs",
        (
            """CREATE TABLE IF NOT EXISTS command_sync_state (
                   scope       TEXT PRIMARY KEY,
                   fingerprint TEXT NOT NULL,
                   synced_at   TEXT NOT NULL
               )""",
        ),
    ),
)


class Database:
    """Owns the process's only connection to the SQLite file."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 10_000) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database.connect() has not been awaited")
        return self._conn

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = await aiosqlite.connect(self._path)
        except Exception as exc:
            raise DatabaseError(f"Cannot open database at {self._path}: {exc}") from exc

        self._conn.row_factory = aiosqlite.Row
        # WAL lets readers proceed during writes; busy_timeout replaces an
        # instant "database is locked" error with a bounded wait.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.commit()
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- Migrations ---

    async def migrate(self) -> None:
        conn = self.connection
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        await conn.commit()

        async with conn.execute("SELECT version FROM schema_migrations") as cursor:
            applied = {row[0] for row in await cursor.fetchall()}

        for version, description, statements in MIGRATIONS:
            if version in applied:
                continue
            logger.info("Applying migration %s: %s", version, description)
            try:
                for statement in statements:
                    if isinstance(statement, str):
                        await conn.execute(statement)
                    else:
                        sql, params = statement
                        await conn.execute(sql, params)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, datetime('now'))",
                    (version,),
                )
                await conn.commit()
            except Exception as exc:
                await conn.rollback()
                raise DatabaseError(f"Migration {version} ({description}) failed: {exc}") from exc

    async def schema_version(self) -> int:
        async with self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # --- Query helpers ---

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write and return the number of affected rows."""
        cursor = await self.connection.execute(sql, params)
        await self.connection.commit()
        return cursor.rowcount

    async def execute_many(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        await self.connection.executemany(sql, params)
        await self.connection.commit()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self.connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def exists(self, sql: str, params: Sequence[Any] = ()) -> bool:
        return await self.fetch_one(sql, params) is not None
