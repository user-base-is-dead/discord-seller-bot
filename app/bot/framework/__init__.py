"""A small aiogram-compatible layer over discord.py.

The store's handlers were written for aiogram. Instead of rewriting their logic for Discord, this
package offers the same surface — `Router`, `F`, filters, `CallbackData`, FSM, `Message.answer()`,
`CallbackQuery.answer()` — and implements it on Discord DMs, buttons, slash commands and threads. The
handlers keep their behaviour line for line; only what genuinely differs on Discord lives here.
"""

from app.bot.framework.bot import Bot
from app.bot.framework.magic import F, MagicFilter
from app.bot.framework.router import UNHANDLED, BaseMiddleware, Dispatcher, Router, SkipHandler

__all__ = [
    "UNHANDLED",
    "BaseMiddleware",
    "Bot",
    "Dispatcher",
    "F",
    "MagicFilter",
    "Router",
    "SkipHandler",
]
