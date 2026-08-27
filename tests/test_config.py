import pytest

from core.config import Config, ConfigError

BASE = {
    "token": "abc",
    "guild_id": 1,
    "staff_logs_channel_id": 2,
    "mod_chat_channel_id": 3,
    "content_channel_id": 4,
    "reddit_channel_id": 5,
    "role_select_channel_id": 6,
    "admin_role_id": 7,
    "moderator_role_id": 8,
    "new_member_role_id": 9,
    "unverified_role_id": 10,
    "reddit_notification_role_id": 11,
    "hadithcritic_user_id": 12,
    "navid_warning_user_id": 13,
    "database_path": "databases/bot.db",
    "log_dir": "logs",
    "image_folder": "resources/Images",
    "navid_warning_image": "resources/navid_warning.webp",
    "debate_map_file": "databases/debate_image_map.json",
    "rules_channel_url": "https://example.invalid",
    "global_command_sync": False,
    "log_level": "INFO",
    "legal_site_base_url": "https://example.invalid",
    "terms_of_service_url": "https://example.invalid/tos",
    "privacy_policy_url": "https://example.invalid/privacy",
    "owner_user_id": 14,
    "archive_dir": "archives",
    "new_member_duration_hours": 24,
    "suspicious_account_max_age_hours": 24,
    "archive_attachment_max_mb": 100,
    "security_notice_enabled": True,
}


def make(**overrides):
    return Config(**{**BASE, **overrides})


def test_base_fixture_covers_every_config_field():
    """Guards against a new field being added without validation coverage."""
    from dataclasses import fields

    assert {f.name for f in fields(Config)} == set(BASE)


pytestmark = pytest.mark.unit


def test_valid_config_passes():
    make().validate()


def test_missing_token_is_reported():
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        make(token="").validate()


def test_zero_id_is_reported():
    with pytest.raises(ConfigError, match="GUILD_ID"):
        make(guild_id=0).validate()


def test_duplicate_channel_ids_are_reported():
    with pytest.raises(ConfigError, match="duplicates"):
        make(content_channel_id=2).validate()


def test_bad_log_level_is_reported():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        make(log_level="CHATTY").validate()


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        make(token="", guild_id=0, log_level="NOPE").validate()
    message = str(excinfo.value)
    assert "DISCORD_TOKEN" in message
    assert "GUILD_ID" in message
    assert "LOG_LEVEL" in message


def test_token_is_not_in_the_repr():
    assert "abc" not in repr(make(token="abc"))


def test_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "999")
    monkeypatch.setenv("GLOBAL_COMMAND_SYNC", "true")
    config = Config.from_env()
    assert config.guild_id == 999
    assert config.global_command_sync is True


def test_from_env_rejects_non_numeric_ids(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "not-a-number")
    with pytest.raises(ConfigError, match="GUILD_ID"):
        Config.from_env()


def test_non_https_legal_urls_are_reported():
    with pytest.raises(ConfigError, match="TERMS_OF_SERVICE_URL"):
        make(terms_of_service_url="http://example.invalid/tos").validate()
    with pytest.raises(ConfigError, match="PRIVACY_POLICY_URL"):
        make(privacy_policy_url="not-a-url").validate()


def test_zero_owner_id_is_reported():
    with pytest.raises(ConfigError, match="OWNER_USER_ID"):
        make(owner_user_id=0).validate()


def test_non_positive_attachment_limit_is_reported():
    with pytest.raises(ConfigError, match="ARCHIVE_ATTACHMENT_MAX_MB"):
        make(archive_attachment_max_mb=0).validate()


def test_policy_urls_derive_from_the_base(monkeypatch):
    """Renaming the repository must be a one-value change."""
    monkeypatch.setenv("LEGAL_SITE_BASE_URL", "https://example.invalid/19bot/")
    monkeypatch.delenv("TERMS_OF_SERVICE_URL", raising=False)
    monkeypatch.delenv("PRIVACY_POLICY_URL", raising=False)
    config = Config.from_env()
    assert config.terms_of_service_url == "https://example.invalid/19bot/terms.html"
    assert config.privacy_policy_url == "https://example.invalid/19bot/privacy.html"


def test_explicit_policy_urls_still_win(monkeypatch):
    monkeypatch.setenv("LEGAL_SITE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("PRIVACY_POLICY_URL", "https://wikisubmission.org/19bot/privacy")
    assert Config.from_env().privacy_policy_url == "https://wikisubmission.org/19bot/privacy"


def test_non_https_base_url_is_reported():
    with pytest.raises(ConfigError, match="LEGAL_SITE_BASE_URL"):
        make(legal_site_base_url="http://example.invalid").validate()
