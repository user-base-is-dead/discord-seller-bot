from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.staff import sent_by_a_person
from app.bot.framework import F, Router
from app.bot.framework.types import Message
from app.core.config import get_settings
from app.database.models.support import TicketStatus
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.locales.i18n import t
from app.services.support_service import relay_staff_message, relay_user_message
from app.utils.errors import UserError

# Registered LAST in main.py — this only ever sees a DM/group message that no earlier,
# more specific handler (commands, menu buttons, FSM wizard steps) already claimed. That's
# what makes "just reply to chat with support" work without a dedicated keyboard button.

router = Router(name="support.relay")


def _extract_attachments(message: Message) -> tuple[str | None, list[str]]:
    """Extract text and files (photos, videos, documents, voice messages, etc.) from a message.
    Returns (text, file_ids) where each file is stored as "type:url" for proper relay.

    Every attachment, in the order it was sent: one Discord message routinely carries several
    screenshots, where a Telegram message carried one photo in several sizes."""
    text = message.text or message.caption
    file_ids = [f"{kind}:{item.file_id}" for kind, item in message.attachments]
    return text, file_ids


async def dm_relay(message: Message, session: AsyncSession, user: User | None = None) -> None:
    """Any DM that fell through everything else: if the user has an open ticket,
    treat it as a reply to support. Supports text, photos, and documents."""
    if user is None or message.chat.type != "private":
        return

    text, file_ids = _extract_attachments(message)
    if not text and not file_ids:
        return

    ticket = await SupportRepo(session).get_open_for_user(user.id)
    if ticket is None:
        return

    try:
        await relay_user_message(message.bot, session, ticket=ticket, user=user, text=text, attachment_file_ids=file_ids)
    except UserError as exc:
        await message.answer(t(exc.i18n_key, user.locale, **exc.vars))


async def group_relay(message: Message, session: AsyncSession, user: User | None = None) -> None:
    """Any message inside a ticket's forum topic gets mirrored to the ticket owner's DM. Supports
    text, photos, and documents.

    Whoever wrote it: every member of these groups is staff (see app/bot/filters/staff.py), so a new
    team member can answer buyers the moment they are added — no ADMIN_IDS entry needed. Other bots
    and posts made on behalf of a chat are the exception; nobody can be credited with those.

    Two groups qualify: SUPPORT_CHANNEL_ID, and ORDERS_CHANNEL_ID for a cancelled order whose thread has
    become a dispute the buyer is connected to. Topic numbers are per-chat, so which group the message
    came from is part of identifying the ticket — matching on the topic id alone would eventually
    relay a reply to the wrong buyer.
    """
    if user is None or not sent_by_a_person(message):
        return
    settings = get_settings()
    in_support = settings.support_channel_id is not None and message.chat.id == settings.support_channel_id
    in_orders = settings.orders_channel_id is not None and message.chat.id == settings.orders_channel_id
    if not (in_support or in_orders):
        return
    if message.message_thread_id is None:
        return

    text, file_ids = _extract_attachments(message)
    if not text and not file_ids:
        return

    # Commands are staff talking to the bot, not to the buyer.
    if text and text.startswith("/"):
        return

    ticket = await SupportRepo(session).get_by_topic_in_group(
        message.message_thread_id, message.chat.id, is_support_group=in_support
    )
    if ticket is None:
        return
    # An order thread that nobody has connected the buyer to is a log, not a conversation: staff talk
    # about the order in it all the time, and mirroring that to the buyer would leak internal notes.
    if in_orders and ticket.status not in (TicketStatus.OPEN, TicketStatus.PENDING):
        return

    await relay_staff_message(message.bot, session, ticket=ticket, staff_discord_id=user.discord_id, text=text, attachment_file_ids=file_ids)


# Both handlers live on one router, and aiogram stops propagating at the FIRST handler whose
# filters pass — regardless of what that handler returns. So a filterless `group_relay` would
# match every DM too, run, return None, and starve `dm_relay` of every message it exists for.
# The chat-type filters keep the two disjoint; the guards inside each handler are then only
# defense in depth.
router.message.register(group_relay, F.chat.type.in_({"group", "supergroup"}))
router.message.register(dm_relay, F.chat.type == "private")
