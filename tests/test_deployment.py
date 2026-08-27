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


# --- Multi-stage image ---


def dockerfile() -> str:
    return read("Dockerfile")


def runtime_stage() -> str:
    """Everything after the final FROM, i.e. what actually ships."""
    return dockerfile().rsplit("FROM ", 1)[1]


def test_image_is_multi_stage():
    text = dockerfile()
    assert "AS builder" in text
    assert "AS runtime" in text


def test_build_toolchain_is_confined_to_the_builder():
    """A compiler in the runtime image is attack surface and dead weight."""
    text = dockerfile()
    toolchain = "build-base" if "alpine" in text else "build-essential"
    assert toolchain in text
    assert toolchain not in runtime_stage()


def test_runtime_stage_copies_the_prebuilt_venv():
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile()


def test_both_stages_share_one_version_arg():
    """Builder and runtime must never drift onto different interpreters."""
    import re

    froms = re.findall(r"^FROM (\S+)", dockerfile(), re.MULTILINE)
    assert len(froms) == 2, froms
    assert froms[0] == froms[1], froms
    assert "${PYTHON_VERSION}" in froms[0]


def test_builder_verifies_dependencies_install():
    """Better to fail the build than to discover it at deploy time."""
    assert "import discord, aiohttp, aiosqlite, feedparser, dotenv, zoneinfo" in dockerfile()


def test_image_declares_no_ports():
    """The bot is an outbound gateway client and listens for nothing."""
    assert "EXPOSE" not in dockerfile()


def test_image_carries_provenance_labels():
    assert "org.opencontainers.image.source" in dockerfile()


# --- Build context ---


def test_dockerignore_denies_by_default():
    """An exclusion list silently ships whatever is added later."""
    lines = [
        line.strip()
        for line in read(".dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"


def test_dockerignore_allows_exactly_what_the_image_needs():
    text = read(".dockerignore")
    for needed in ("!requirements.txt", "!main.py", "!core/", "!cogs/", "!resources/"):
        assert needed in text, needed


def test_dockerignore_reexcludes_secrets_and_state():
    text = read(".dockerignore")
    for excluded in ("**/.env", "databases/", "archives/", "logs/"):
        assert excluded in text, excluded


def test_no_test_or_dev_files_are_allowed_into_the_context():
    """tests/, scripts/ and site/ are not needed at runtime."""
    text = read(".dockerignore")
    for name in ("!tests", "!scripts", "!site", "!.git"):
        assert name not in text, name


# --- Configuration comes from the environment ---


def test_compose_reads_the_env_file():
    compose = yaml.safe_load(read("docker-compose.yml"))
    assert ".env" in compose["services"]["bot"]["env_file"]


def test_no_token_is_baked_into_the_image_definition():
    for name in ("Dockerfile", "docker-compose.yml"):
        text = read(name)
        assert "DISCORD_TOKEN=" not in text, name


def test_compose_allows_overriding_the_run_user():
    """Bind mounts keep host ownership, so native Linux needs this."""
    assert "${DOCKER_USER:-10001:10001}" in read("docker-compose.yml")


# --- Supply chain ---


def test_runtime_image_has_no_package_manager():
    """pip vendors a flagged msgpack and ensurepip bundles a flagged setuptools.

    Neither is importable by the app, so both are removed. This asserts the
    removal is still there, because it is worth several HIGH findings.
    """
    text = dockerfile()
    assert "pip uninstall" in text
    assert "ensurepip" in text
    assert "site-packages/pip" in text


def test_os_packages_are_patched_at_build_time():
    """The published base image lags its own security updates."""
    text = dockerfile()
    assert ("apk upgrade" in text) or ("apt-get upgrade" in text)


def test_dependency_pins_clear_the_known_advisories():
    """Floors chosen from the scan; see docs/SECURITY.md before lowering."""
    requirements = read("requirements.txt")
    minimums = {
        "aiohttp": (3, 14, 3),
        "python-dotenv": (1, 2, 2),
    }
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        if name in minimums:
            parts = tuple(int(p) for p in version.split(".")[:3])
            assert parts >= minimums[name], f"{name} {version} is below the CVE floor"


# --- Shutdown has a single owner ---


def test_signal_handler_only_signals():
    """Two concurrent shutdowns deadlocked and the container was SIGKILLed."""
    main = read("main.py")
    handler = main.split("def _install_shutdown_handlers")[1].split("\nasync def main")[0]
    assert "stop.set()" in handler
    assert "create_task" not in handler, "the handler must not start a shutdown itself"


def test_shutdown_is_bounded():
    main = read("main.py")
    assert "_SHUTDOWN_TIMEOUT" in main
    assert "_CLIENT_CLOSE_TIMEOUT" in main


def test_shutdown_ceiling_is_under_the_compose_grace_period():
    """Exceeding it means the runtime kills the process instead."""
    import re

    ceiling = float(re.search(r"_SHUTDOWN_TIMEOUT = ([\d.]+)", read("main.py")).group(1))
    compose = yaml.safe_load(read("docker-compose.yml"))
    grace = compose["services"]["bot"]["stop_grace_period"]
    seconds = float(str(grace).rstrip("s"))
    assert ceiling < seconds, f"ceiling {ceiling}s must be under the {seconds}s grace period"


def test_database_is_closed_even_if_the_client_will_not():
    main = read("main.py")
    finally_block = main.rsplit("finally:", 1)[1]
    assert "bot.db.close()" in finally_block
