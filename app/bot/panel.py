from __future__ import annotations

from app.bot.framework.types import ReplyKeyboardRemove


def reset_installed_panels() -> None:
    """Test hook kept for parity with the Telegram bot. Nothing is cached any more."""


def panel_markup(
    discord_id: int, locale: str, *, is_admin: bool, force: bool = False
) -> ReplyKeyboardRemove | None:
    """The Telegram bot used this to take down a retired bottom reply keyboard. Discord has no reply
    keyboards at all, so there is never anything to remove and this always returns None — callers
    that attach its result to a message they were sending anyway simply attach nothing."""
    return None
