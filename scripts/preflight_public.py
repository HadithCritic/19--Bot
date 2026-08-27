"""Verify this working tree is safe to publish as a public repository.

Run this immediately before pushing to a public remote. It inspects what git
would actually publish, not the filesystem, so an ignored file is correctly
treated as absent.

    python scripts/preflight_public.py

Exit code 0 means nothing sensitive was found. Non-zero means do not push.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Paths that must never be tracked.
FORBIDDEN_PATHS = (
    (re.compile(r"(^|/)\.env$"), "environment file with credentials"),
    (re.compile(r"\.(db|db-wal|db-shm|sqlite|sqlite3)$"), "database with member data"),
    # Anything under the state folders except the two allowlisted files. These
    # folders are deny-by-default in .gitignore for the same reason.
    (
        re.compile(r"^databases/(?!\.gitkeep$|debate_image_map\.json$)"),
        "runtime state that may contain member data",
    ),
    (re.compile(r"^archives/(?!\.gitkeep$)"), "channel archive with member data"),
    (re.compile(r"^backups/"), "local database backup"),
    (re.compile(r"^logs/|\.log$"), "log file"),
    (re.compile(r"^\.venv/|^venv/|^env/"), "virtual environment"),
    (re.compile(r"^\.claude/settings\.local\.json$"), "machine-local settings"),
    (re.compile(r"^\.(vscode|idea)/"), "machine-local editor settings"),
)

# Credential shapes worth failing on. Deliberately narrow: a scanner that cries
# wolf gets ignored, which is worse than no scanner.
SECRET_PATTERNS = (
    (
        re.compile(r"\b[MN][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}\b"),
        "Discord bot token",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI-style API key"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), "private key"),
    (
        # Must include a scheme and "//": scheme://user:pass@host. Anchoring on
        # "://" keeps ordinary mailto: links and plain email addresses out of the
        # results, which the earlier pattern flagged on every contact address.
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s:@/]{4,}@[A-Za-z0-9.-]+"),
        "credentials embedded in a URL",
    ),
)

# Files whose whole purpose is to describe or exercise secret shapes.
SELF_EXEMPT = {"scripts/preflight_public.py", "tests/test_preflight.py"}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".yml",
    ".yaml",
    ".html",
    ".css",
    ".bat",
    ".cfg",
    ".ini",
    ".sh",
    ".example",
    "",
}


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def tracked_files() -> list[str]:
    return [line for line in git("ls-files").splitlines() if line.strip()]


def check_paths(files: list[str]) -> list[str]:
    """One finding per offending path, even when several rules match it.

    databases/bot.db matches both the extension rule and the folder rule; a
    reader should see it once.
    """
    problems = []
    for path in files:
        for pattern, description in FORBIDDEN_PATHS:
            if pattern.search(path):
                problems.append(f"{path} is tracked ({description})")
                break
    return problems


def check_contents(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if path in SELF_EXEMPT:
            continue
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        full = ROOT / path
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, description in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{path}:{line} looks like a {description}")
    return problems


def _sensitive_paths_in(*rev_args: str) -> dict[str, list[str]]:
    """Group sensitive paths added anywhere in the given revision range."""
    found: dict[str, list[str]] = {}
    log = git("log", *rev_args, "--pretty=format:", "--name-only", "--diff-filter=A")
    for path in sorted({line.strip() for line in log.splitlines() if line.strip()}):
        for pattern, description in FORBIDDEN_PATHS:
            if pattern.search(path):
                found.setdefault(description, []).append(path)
                break
    return found


def check_history() -> dict[str, list[str]]:
    """Sensitive paths in the history a normal push would publish.

    Scoped to HEAD rather than --all: a plain `git push` publishes the current
    branch, so an unrelated local branch must not fail this check. Other refs
    are reported separately by check_other_refs.
    """
    return _sensitive_paths_in("HEAD")


def check_other_refs() -> dict[str, list[str]]:
    """Sensitive paths reachable from local refs other than HEAD.

    These are not published by a normal push, only by `push --all`, `--mirror`,
    or pushing that branch explicitly. Worth knowing about, not worth blocking.
    """
    current = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    refs = [
        line.strip()
        for line in git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags"
        ).splitlines()
        if line.strip() and line.strip() != current
    ]
    if not refs:
        return {}
    return _sensitive_paths_in(*refs, "--not", "HEAD")


def main() -> int:
    files = tracked_files()
    print(f"Inspecting {len(files)} tracked file(s)\n")

    blocking = check_paths(files) + check_contents(files)
    history = check_history()
    other_refs = check_other_refs()

    if blocking:
        print("BLOCKING — do not push to a public remote:")
        for item in blocking:
            print(f"  - {item}")
        print()

    if history:
        total = sum(len(paths) for paths in history.values())
        print(f"HISTORY — {total} sensitive file(s) exist in past commits.")
        print("These are NOT in the current tree, but git history is permanent:\n")
        for description in sorted(history, key=lambda key: -len(history[key])):
            paths = history[description]
            print(f"  {len(paths):>4}  {description}")
            for path in paths[:3]:
                print(f"        {path}")
            if len(paths) > 3:
                print(f"        ... and {len(paths) - 3} more")
        print(
            "\n  Publishing this repository as-is would expose all of the above.\n"
            "  A fresh repository with a single commit removes it entirely.\n"
            "  See docs/VERIFICATION.md for the exact steps."
        )
        print()

    if other_refs:
        total = sum(len(paths) for paths in other_refs.values())
        current = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        print(f"OTHER LOCAL REFS — {total} sensitive file(s) outside {current}.")
        print("A normal push does not publish these. Never use push --all or")
        print("--mirror on this repository, and do not push those branches.")
        print()
        for description in sorted(other_refs, key=lambda key: -len(other_refs[key])):
            print(f"  {len(other_refs[description]):>4}  {description}")
        print()

    if not blocking and not history:
        print("PASS — nothing sensitive is tracked, and the history this branch")
        print("would publish is clean.")
        return 0

    if blocking:
        print("Result: FAIL")
        return 1

    print("Result: current tree is clean, but history is not. Publish only after")
    print("recreating the repository with no history.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
