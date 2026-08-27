"""Root logging configuration.

Nothing configured logging before this module existed, so every logger.error()
in the codebase was discarded and the bot could fail silently for weeks. This
attaches a rotating file handler plus a UTF-8 console handler, which also
removes the need for the Windows stdout re-wrapping hack in main.py.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)-24s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_configured = False

# discord.py warns at startup that PyNaCl and davey are missing, meaning voice
# is unsupported. That is deliberate: the voice cog was removed and the deps
# dropped, so the warning describes an intended state and would otherwise
# appear on every start as if something were wrong.
_EXPECTED_STARTUP_WARNINGS = ("voice will NOT be supported",)


class _DropExpectedVoiceWarnings(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(text in message for text in _EXPECTED_STARTUP_WARNINGS)


def setup_logging(*, log_dir: str = "logs", level: str = "INFO") -> None:
    """Configure the root logger. Safe to call more than once."""
    global _configured
    if _configured:
        return

    resolved_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    # Emoji in log records must not crash the Windows console encoder.
    if hasattr(console.stream, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            console.stream.reconfigure(encoding="utf-8", errors="replace")
    root.addHandler(console)

    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / "bot.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("File logging disabled, cannot write to %s: %s", log_dir, exc)

    # discord.py is chatty at INFO on every gateway event.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").addFilter(_DropExpectedVoiceWarnings())
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    _configured = True
