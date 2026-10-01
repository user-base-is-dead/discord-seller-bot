"""The bot's error taxonomy, independent of the chat platform.

Handlers and services catch these instead of discord.py's own exceptions, so the same `except
BotAPIError` that guarded a Telegram call guards a Discord one. `translate()` is the single place a
`discord.HTTPException` becomes one of them.
"""

from __future__ import annotations

import discord


class BotAPIError(Exception):
    """Any failure talking to the chat platform."""

    def __init__(self, message: str = "", *, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.original = original


class BotBadRequest(BotAPIError):
    """The request itself was refused — a message that can't be edited, an expired interaction."""


class BotNotFound(BotBadRequest):
    """The channel, thread, message or user no longer exists (or never did)."""


class BotForbiddenError(BotAPIError):
    """Missing permissions, or a user who does not accept DMs from this bot."""


class BotRetryAfter(BotAPIError):
    def __init__(self, message: str = "", *, retry_after: float = 1.0, original=None) -> None:
        super().__init__(message, original=original)
        self.retry_after = retry_after


# Discord's "Unknown interaction": the 3-second window was missed or the token (15 minutes) expired.
# Reported with Telegram's wording so the error middleware keeps treating it as harmless noise.
_UNKNOWN_INTERACTION = 10062


def translate(exc: BaseException) -> BotAPIError:
    if isinstance(exc, BotAPIError):
        return exc
    if isinstance(exc, discord.Forbidden):
        return BotForbiddenError(f"Forbidden: {exc.text or exc}", original=exc)
    if isinstance(exc, discord.NotFound):
        if getattr(exc, "code", None) == _UNKNOWN_INTERACTION:
            return BotBadRequest("Bad Request: query is too old", original=exc)
        return BotNotFound(f"Not Found: {exc.text or exc}", original=exc)
    if isinstance(exc, discord.HTTPException):
        if exc.status == 429:
            retry = float(getattr(exc, "retry_after", 1.0) or 1.0)
            return BotRetryAfter(f"Too Many Requests: {exc.text}", retry_after=retry, original=exc)
        if 400 <= exc.status < 500:
            return BotBadRequest(f"Bad Request: {exc.text or exc}", original=exc)
        return BotAPIError(f"Discord error {exc.status}: {exc.text or exc}", original=exc)
    if isinstance(exc, discord.ClientException):
        return BotBadRequest(f"Bad Request: {exc}", original=exc)
    return BotAPIError(str(exc), original=exc)
