"""Tests for deployment consistency.

The bot has to behave the same on Windows, macOS, Linux and inside the
container, so the version, the writable directories and the healthcheck are
asserted here rather than trusted.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# --- One Python version everywhere ---


def declared_version() -> str:
    return read(".python-version").strip()


def test_python_version_file_exists_and_is_a_version():
    assert re.fullmatch(r"\d+\.\d+", declared_version())


def test_dockerfile_uses_the_declared_version():
    assert f"ARG PYTHON_VERSION={declared_version()}" in read("Dockerfile")


def test_compose_build_arg_matches():
    compose = yaml.safe_load(read("docker-compose.yml"))
    args = compose["services"]["bot"]["build"]["args"]
    assert str(args["PYTHON_VERSION"]) == declared_version()


def test_pyproject_floor_matches():
    assert f'requires-python = ">={declared_version()}"' in read("pyproject.toml")


def test_ruff_target_matches():
    major, minor = declared_version().split(".")
    assert f'target-version = "py{major}{minor}"' in read("pyproject.toml")


def test_pyright_version_matches():
    assert json.loads(read("pyrightconfig.json"))["pythonVersion"] == declared_version()


def test_workflows_read_the_version_from_the_file():
    """Duplicating the number in CI is how these drift apart."""
    for name in ("ci.yml", "pages.yml"):
        text = read(f".github/workflows/{name}")
        assert "python-version-file: .python-version" in text, name
        assert 'python-version: "' not in text, name


# --- Every directory the bot writes to is persisted ---


WRITABLE_DIRS = ("databases", "logs", "archives")


def test_compose_mounts_every_writable_directory():
    """archives/ was missing, so channel exports vanished on recreate."""
    compose = yaml.safe_load(read("docker-compose.yml"))
    volumes = compose["services"]["bot"]["volumes"]
    for directory in WRITABLE_DIRS:
        assert any(f":/app/{directory}" in v for v in volumes), directory


def test_dockerfile_creates_and_owns_every_writable_directory():
    """A non-root process cannot create these under /app at runtime."""
    dockerfile = read("Dockerfile")
    for directory in WRITABLE_DIRS:
        assert f"/app/{directory}" in dockerfile, directory
    assert "chown -R botuser:botuser" in dockerfile


def test_container_does_not_run_as_root():
    assert "USER botuser" in read("Dockerfile")


# --- Healthcheck matches what the bot actually writes ---


def test_healthcheck_watches_the_heartbeat_file():
    dockerfile = read("Dockerfile")
    assert "HEALTHCHECK" in dockerfile
    assert "heartbeat" in dockerfile
    # Log freshness was the old signal and gave false negatives on a quiet bot.
    assert "bot.log" not in dockerfile


def test_bot_writes_the_heartbeat_the_healthcheck_reads():
    main = read("main.py")
    assert 'Path(CONFIG.log_dir) / "heartbeat"' in main
    assert "self.heartbeat.start()" in main
    assert "self.heartbeat.cancel()" in main


def test_healthcheck_tolerance_exceeds_the_write_interval():
    """A tolerance below the interval would flap between healthy and not."""
    main = read("main.py")
    interval = int(re.search(r"_HEARTBEAT_SECONDS = (\d+)", main).group(1))
    tolerance = int(re.search(r"st_mtime < (\d+)", read("Dockerfile")).group(1))
    assert tolerance > interval * 2


# --- Graceful shutdown ---


def test_compose_uses_init_for_signal_forwarding():
    compose = yaml.safe_load(read("docker-compose.yml"))
    assert compose["services"]["bot"]["init"] is True


def test_sigterm_is_handled():
    """docker stop sends SIGTERM; the default action skips database cleanup."""
    main = read("main.py")
    assert "SIGTERM" in main
    assert "add_signal_handler" in main


# --- Line endings and launchers ---


def test_gitattributes_normalises_line_endings():
    text = read(".gitattributes")
    assert "* text=auto eol=lf" in text
    # cmd.exe needs CRLF in .bat files.
    assert "*.bat text eol=crlf" in text


def test_both_launchers_exist():
    assert (ROOT / "start_bot.bat").is_file()
    assert (ROOT / "start_bot.sh").is_file()


def test_launchers_agree_on_the_exit_codes():
    """Exit 2 means do not restart; both launchers must honour that."""
    for name in ("start_bot.bat", "start_bot.sh"):
        assert "2" in read(name), name
    assert "ERRORLEVEL%==2" in read("start_bot.bat")
    assert '"$status" -eq 2' in read("start_bot.sh")


def test_shell_launcher_has_no_crlf():
    """CRLF in a shell script fails with 'bad interpreter' on Linux."""
    assert b"\r\n" not in (ROOT / "start_bot.sh").read_bytes()
