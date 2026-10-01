from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path("bot.log")
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# A Discord bot token: three base64url segments joined by dots (user id . timestamp . HMAC).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
_REDACTED = "***REDACTED***"
_URL_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+:[^@\s]+@")


class _DropVoiceNotice(logging.Filter):
    """discord.py warns at every start that the voice libraries (PyNaCl, davey) are missing. The store
    has no voice features, and a WARNING line on a healthy start reads like a fault."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


class SecretRedactionFilter(logging.Filter):
    """Strips bot tokens / DB URLs with credentials out of log lines before they hit any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _TOKEN_RE.search(msg) or ("://" in msg and "@" in msg):
            # Redacted after formatting, so the rest of the line (its arguments) survives intact.
            record.msg = _URL_CREDENTIALS_RE.sub("://***:***@", _TOKEN_RE.sub(_REDACTED, msg))
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redaction = SecretRedactionFilter()

    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redaction)
    root.addHandler(stream)

    # A crash surfaces to the user as a one-line "something went wrong" alert; the traceback that
    # explains it only ever existed in the console, which is gone the moment the window is closed
    # or scrolled. Rotating file so it cannot grow unbounded on a long-running bot.
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
    except OSError:
        # A read-only or otherwise unwritable working directory must not stop the bot booting.
        root.warning("Could not open %s for logging; console only", LOG_FILE)
    else:
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    # discord.py logs every gateway heartbeat/resume at INFO; keep the log about the store.
    logging.getLogger("discord").setLevel(logging.WARNING)
    discord_client = logging.getLogger("discord.client")
    discord_client.setLevel(logging.INFO)
    discord_client.addFilter(_DropVoiceNotice())

    # Every SQL statement, only when asked for with LOG_LEVEL=DEBUG. It used to be on in every
    # non-production run through the engine's `echo`, which printed each statement twice (its own
    # handler plus this one), buried real errors under hundreds of lines, and filled bot.log fast
    # enough to rotate the tracebacks worth keeping out of it.
    if root.level <= logging.DEBUG:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
