"""Typed configuration, loaded from the environment once at import time.

Every Discord ID has a default matching the current deployment, so the bot runs
without a fully populated .env, while still allowing any value to be overridden
per environment. Call validate() during startup to fail fast on bad values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Discord ID fields that must be non-zero for the bot to behave correctly.
_REQUIRED_IDS = (
    "guild_id",
    "staff_logs_channel_id",
    "mod_chat_channel_id",
    "content_channel_id",
    "reddit_channel_id",
    "admin_role_id",
    "moderator_role_id",
    "new_member_role_id",
)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration."""

    token: str = field(repr=False)

    # Guild
    guild_id: int

    # Channels
    staff_logs_channel_id: int
    mod_chat_channel_id: int
    content_channel_id: int
    reddit_channel_id: int
    role_select_channel_id: int

    # Roles
    admin_role_id: int
    moderator_role_id: int
    new_member_role_id: int
    unverified_role_id: int
    reddit_notification_role_id: int

    # Users
    owner_user_id: int
    hadithcritic_user_id: int
    navid_warning_user_id: int

    # Paths
    database_path: str
    log_dir: str
    image_folder: str
    navid_warning_image: str
    debate_map_file: str
    archive_dir: str

    # URLs
    rules_channel_url: str
    # Base of the published policy site. The two policy URLs derive from it, so
    # renaming the repository is a one-value change.
    legal_site_base_url: str
    terms_of_service_url: str
    privacy_policy_url: str

    # Behaviour
    global_command_sync: bool
    log_level: str
    new_member_duration_hours: int
    suspicious_account_max_age_hours: int
    archive_attachment_max_mb: int
    security_notice_enabled: bool

    @classmethod
    def from_env(cls) -> Config:
        # GitHub Pages URL for the public repository serving site/. Change this
        # one value (or set LEGAL_SITE_BASE_URL) if the repository is renamed.
        base = _env_str("LEGAL_SITE_BASE_URL", "https://hadithcritic.github.io/19--Bot").rstrip("/")
        return cls(
            token=_env_str("DISCORD_TOKEN", ""),
            guild_id=_env_int("GUILD_ID", 576134563496198144),
            staff_logs_channel_id=_env_int("STAFF_LOGS_CHANNEL_ID", 1373484140942590083),
            mod_chat_channel_id=_env_int("MOD_CHAT_CHANNEL_ID", 1371952659451347004),
            content_channel_id=_env_int("CONTENT_CHANNEL_ID", 1371667748479828100),
            reddit_channel_id=_env_int("REDDIT_CHANNEL_ID", 1385292566546878485),
            role_select_channel_id=_env_int("ROLE_SELECT_CHANNEL_ID", 1373470762924511352),
            admin_role_id=_env_int("ADMIN_ROLE_ID", 1371602096784408606),
            moderator_role_id=_env_int("MODERATOR_ROLE_ID", 1371666296076370031),
            new_member_role_id=_env_int("NEW_MEMBER_ROLE_ID", 1442499119443611648),
            unverified_role_id=_env_int("UNVERIFIED_ROLE_ID", 1377160771070591016),
            reddit_notification_role_id=_env_int(
                "REDDIT_NOTIFICATION_ROLE_ID", 1405653140476268604
            ),
            owner_user_id=_env_int("OWNER_USER_ID", 282766915242295296),
            hadithcritic_user_id=_env_int("HADITHCRITIC_USER_ID", 282766915242295296),
            navid_warning_user_id=_env_int("NAVID_WARNING_USER_ID", 426631409461886976),
            database_path=_env_str("DATABASE_PATH", "databases/bot.db"),
            log_dir=_env_str("LOG_DIR", "logs"),
            image_folder=_env_str("IMAGE_FOLDER", "resources/Images"),
            navid_warning_image=_env_str("NAVID_WARNING_IMAGE", "resources/navid_warning.webp"),
            debate_map_file=_env_str("DEBATE_MAP_FILE", "databases/debate_image_map.json"),
            archive_dir=_env_str("ARCHIVE_DIR", "archives"),
            rules_channel_url=_env_str(
                "RULES_CHANNEL_URL",
                "https://discord.com/channels/576134563496198144/1372353028367192107",
            ),
            legal_site_base_url=base,
            terms_of_service_url=_env_str("TERMS_OF_SERVICE_URL", f"{base}/terms.html"),
            privacy_policy_url=_env_str("PRIVACY_POLICY_URL", f"{base}/privacy.html"),
            global_command_sync=_env_bool("GLOBAL_COMMAND_SYNC", False),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            new_member_duration_hours=_env_int("NEW_MEMBER_DURATION_HOURS", 24),
            suspicious_account_max_age_hours=_env_int("SUSPICIOUS_ACCOUNT_MAX_AGE_HOURS", 24),
            archive_attachment_max_mb=_env_int("ARCHIVE_ATTACHMENT_MAX_MB", 100),
            security_notice_enabled=_env_bool("SECURITY_NOTICE_ENABLED", True),
        )

    def validate(self) -> None:
        """Raise ConfigError describing every problem found, not just the first."""
        problems: list[str] = []

        if not self.token:
            problems.append("DISCORD_TOKEN is not set (see .env.example)")

        for name in _REQUIRED_IDS:
            if getattr(self, name) <= 0:
                problems.append(f"{name.upper()} must be a positive Discord ID")

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level not in valid_levels:
            problems.append(f"LOG_LEVEL must be one of {sorted(valid_levels)}")

        if self.new_member_duration_hours <= 0:
            problems.append("NEW_MEMBER_DURATION_HOURS must be positive")

        if self.suspicious_account_max_age_hours <= 0:
            problems.append("SUSPICIOUS_ACCOUNT_MAX_AGE_HOURS must be positive")

        if self.owner_user_id <= 0:
            problems.append("OWNER_USER_ID must be a positive Discord ID")

        if self.archive_attachment_max_mb <= 0:
            problems.append("ARCHIVE_ATTACHMENT_MAX_MB must be positive")

        # Required for Discord app verification, and surfaced by /privacy.
        # Discord fetches both URLs and rejects anything it cannot reach.
        for name in ("legal_site_base_url", "terms_of_service_url", "privacy_policy_url"):
            value = getattr(self, name)
            if not value.startswith("https://"):
                problems.append(f"{name.upper()} must be an https:// URL")

        # Catch copy-paste mistakes where two distinct IDs end up identical.
        id_fields = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name.endswith(("_channel_id", "_role_id"))
        }
        seen: dict[int, str] = {}
        for name, value in id_fields.items():
            if value in seen:
                problems.append(f"{name.upper()} duplicates {seen[value].upper()} ({value})")
            else:
                seen[value] = name

        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


CONFIG = Config.from_env()
