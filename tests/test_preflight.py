"""Tests for the public-repository pre-flight check.

This script is the last gate before member data and credentials could become
public, so its detection has to be right.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from preflight_public import FORBIDDEN_PATHS, SECRET_PATTERNS, check_paths

pytestmark = pytest.mark.unit


def matches(path: str) -> bool:
    return any(pattern.search(path) for pattern, _ in FORBIDDEN_PATHS)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env",
        "databases/bot.db",
        "databases/bot.db-wal",
        "archives/123/general/messages.jsonl",
        "backups/after_ban.db",
        "logs/bot.log",
        "logs/messages_2025-06-05.json",
        "bot.log",
        ".venv/Scripts/python.exe",
        ".vscode/settings.json",
        ".claude/settings.local.json",
        "databases/blog_data.json",
    ],
)
def test_sensitive_paths_are_caught(path):
    assert matches(path), path


@pytest.mark.parametrize(
    "path",
    [
        "main.py",
        "core/config.py",
        "docs/PRIVACY_POLICY.md",
        "site/privacy.html",
        ".env.example",
        "databases/.gitkeep",
        "databases/debate_image_map.json",
        "resources/navid_warning.webp",
        "requirements.txt",
        ".github/workflows/ci.yml",
    ],
)
def test_legitimate_paths_are_not_flagged(path):
    assert not matches(path), path


def test_check_paths_reports_each_offender():
    problems = check_paths([".env", "main.py", "databases/bot.db"])
    assert len(problems) == 2
    assert any(".env" in p for p in problems)
    assert any("bot.db" in p for p in problems)


def test_check_paths_is_quiet_on_a_clean_tree():
    assert check_paths(["main.py", "core/config.py", "site/index.html"]) == []


def secret_hits(text: str) -> list[str]:
    return [label for pattern, label in SECRET_PATTERNS if pattern.search(text)]


def test_discord_token_shape_is_detected():
    token = "MTM3" + "a" * 22 + ".abcdef." + "b" * 30
    assert "Discord bot token" in secret_hits(f"DISCORD_TOKEN={token}")


def test_github_token_shape_is_detected():
    assert "GitHub token" in secret_hits("ghp_" + "a" * 36)


def test_private_key_header_is_detected():
    assert "private key" in secret_hits("-----BEGIN RSA PRIVATE KEY-----")


def test_aws_key_is_detected():
    assert "AWS access key id" in secret_hits("AKIA" + "A" * 16)


def test_ordinary_code_is_not_flagged():
    """False positives get the scanner ignored, which is worse than none."""
    for benign in (
        "GUILD_ID = 576134563496198144",
        "https://hadithcritic.github.io/19Embed/privacy.html",
        "someone@example.org",
        "await member.timeout(discord.utils.utcnow() + _MAX_TIMEOUT)",
        "sha256 fingerprint of the command payloads",
        "https://www.reddit.com/r/HadithCriticism/.rss",
    ):
        assert secret_hits(benign) == [], benign


# --- The state folders are deny-by-default ---


@pytest.mark.parametrize(
    "path",
    [
        # Extension lists miss these; the folder rule does not.
        "databases/bot.db",
        "databases/dump.sqlite",
        "databases/export.csv",
        "databases/members.jsonl",
        "databases/secrets.txt",
        "databases/blog_data.json",
        "databases/blog_data.json.migrated",
        "archives/576134563496198144/general-1/messages.jsonl",
        "archives/576/general/attachments/1-2-img.png",
        "backups/anything.at.all",
    ],
)
def test_state_folder_contents_are_caught(path):
    assert matches(path), path


@pytest.mark.parametrize(
    "path",
    [
        # The only two files in databases/ that are safe to publish.
        "databases/.gitkeep",
        "databases/debate_image_map.json",
        "archives/.gitkeep",
    ],
)
def test_allowlisted_state_files_are_permitted(path):
    assert not matches(path), path


def test_a_path_matching_several_rules_is_reported_once():
    """databases/bot.db matches both the extension rule and the folder rule."""
    problems = check_paths(["databases/bot.db"])
    assert len(problems) == 1


def test_contact_addresses_are_not_mistaken_for_credentials():
    """The policy documents are full of mailto: links and an email address."""
    for benign in (
        "mailto:someone@example.org",
        '<a href="mailto:someone@example.org">contact</a>',
        "Contact: someone@example.org",
        "reachable at someone@example.org. There is no company",
    ):
        assert secret_hits(benign) == [], benign


def test_real_url_credentials_are_still_caught():
    assert "credentials embedded in a URL" in secret_hits(
        "postgres://admin:sup3rs3cret@db.example.org:5432/app"
    )
