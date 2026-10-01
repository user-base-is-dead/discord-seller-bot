"""Who counts as staff inside the support channel and the orders channel.

Those two channels are where the team works: every ticket and every order has its own thread there.
Access is the staff list — whoever the owner lets see a channel (a Staff role on a private channel)
can reply to buyers, close threads, settle warranty claims and fulfil or decline orders from inside
it, without an ADMIN_IDS entry. Only people who can see a private channel can post in it or press
the buttons in it, which makes the channel an update comes from the credential — the same rule the
Telegram bot applied to its private staff groups. Keep both channels private to the team.

Everything else — the admin panel, settling or unfreezing refund money, users, products,
broadcasts — stays behind IsAdmin, in the channels and out of them.
"""

from __future__ import annotations

from typing import Any

from app.bot.filters.is_admin import IsAdmin
from app.bot.framework.filters import Filter
from app.bot.framework.types import BotObject, CallbackQuery, Message
from app.core.config import get_settings


def is_staff_group(chat_id: int | None) -> bool:
    """SUPPORT_CHANNEL_ID or ORDERS_CHANNEL_ID — the two channels whose members are staff. A thread
    reports its parent channel as its chat, so every ticket and order thread counts too."""
    if chat_id is None:
        return False
    settings = get_settings()
    return chat_id in (settings.support_channel_id, settings.orders_channel_id)


def sent_by_a_person(message: Message) -> bool:
    """A human typing as themselves.

    Not other bots, and not webhook posts (an integration, a crosspost) — there is nobody behind
    those to credit the reply to.
    """
    sender = message.from_user
    return message.sender_chat is None and sender is not None and not sender.is_bot


class InStaffGroup(Filter):
    """A person acting inside the support or orders channel (or one of their threads)."""

    async def __call__(self, event: BotObject, **_: Any) -> bool:
        if isinstance(event, Message):
            return sent_by_a_person(event) and is_staff_group(event.chat.id)
        if isinstance(event, CallbackQuery):
            # Only people press buttons, so there is no sender to rule out — just the channel.
            chat = event.message.chat if event.message is not None else None
            return chat is not None and is_staff_group(chat.id)
        return False


class IsStaff(Filter):
    """Anyone inside the staff channels, and admins anywhere — an admin can still do the same work
    from their DMs with the bot."""

    async def __call__(self, event: BotObject, **data: Any) -> bool:
        if await InStaffGroup()(event, **data):
            return True
        return await IsAdmin()(event, **data)
