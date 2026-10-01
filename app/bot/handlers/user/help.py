from __future__ import annotations

from app.bot.framework import Router
from app.bot.framework.filters import Command
from app.bot.framework.types import Message
from app.database.models.user import User
from app.locales.i18n import t

router = Router(name="user.help")


@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    await message.answer(t("help.body", user.locale))
