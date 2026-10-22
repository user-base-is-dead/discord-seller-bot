from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.framework import Bot
from app.bot.framework.exceptions import BotAPIError
from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode
from app.database.models.order import Delivery, FundingSource, Order, OrderStatus, RefundState
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind
from app.database.models.support import TicketStatus
from app.database.repositories.order_event_repo import OrderEventRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.user_repo import UserRepo
from app.services.staff_guide import order_guide
from app.utils.money import format_minor
from app.utils.text import escape_html, mention, pack_messages, thumbnail, timestamp

logger = logging.getLogger(__name__)

# One heading per event kind, written for somebody scrolling the thread rather than reading a table.
_EVENT_LINE = {
    OrderEventKind.PLACED: "🛒 Placed",
    OrderEventKind.DELIVERED: "📬 Delivered",
    OrderEventKind.DECLINED: "🚫 Declined",
    OrderEventKind.REFUND_PARKED: "💰 Refund parked",
    OrderEventKind.REFUND_FROZEN: "🧊 Money frozen",
    OrderEventKind.REFUND_UNFROZEN: "🔓 Unfrozen → Refund Wallet",
    OrderEventKind.REFUND_SANCTIONED: "🚫 Refund sanctioned",
    OrderEventKind.SANCTION_RELEASED: "✅ Sanction released → Refund Wallet",
    OrderEventKind.REFUND_PAID_OUT: "📤 Refund sent",
    OrderEventKind.REFUND_MOVED: "➡️ Refund moved to wallet",
    OrderEventKind.TICKET_OPENED: "🎫 Refund ticket",
}

_STATUS_EMOJI = {
    "PENDING": "🟡",
    "PROCESSING": "🔵",
    "COMPLETED": "🟢",
    "CANCELLED": "🔴",
    "FAILED": "⚠️",
}

# What each status means to whoever opens the thread, under the order number.
_STATUS_LABEL = {
    "PENDING": "Pending — waiting for payment",
    "PROCESSING": "Processing — waiting to be fulfilled",
    "COMPLETED": "Completed — delivered",
    "CANCELLED": "Cancelled",
    "FAILED": "Failed",
}


def topic_name(order: Order, who: str) -> str:
    """Discord caps thread names at 100 chars. The order number leads because it is what staff quote
    and search on; the buyer is the human-readable half."""
    return f"{order.order_number} · {who}"[:100]


async def _buyer_handle(session: AsyncSession, order: Order) -> str:
    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer is None:
        return f"user {order.user_id}"
    return f"@{buyer.username}" if buyer.username else f"id {buyer.discord_id}"


async def _deliveries(session: AsyncSession, order: Order) -> dict[int, Delivery]:
    """The latest delivery per item, keyed by order_item id. A re-fulfilled item has more than one
    row and the newest is the one that says where it stands."""
    ids = [item.id for item in order.items]
    if not ids:
        return {}
    rows = await session.execute(
        select(Delivery).where(Delivery.order_item_id.in_(ids)).order_by(Delivery.id)
    )
    return {row.order_item_id: row for row in rows.scalars()}


async def _order_card(bot: Bot, session: AsyncSession, order: Order) -> str:
    """The whole order on one message, so staff never have to leave the group to know what they are
    looking at — and never have to reconstruct it from the event lines underneath.

    Deliberately the same picture as the admin dossier: who bought what, what it cost, how it was
    paid for, whether it has been delivered, why it died, what is owed and whether anybody has been
    paid back. It is edited in place on every action (see `sync`), so it reads as the order's current
    state rather than as the moment it was placed.

    Laid out as a card: the order number as its heading with the buyer's avatar beside it, then one
    block each for who and when, what was bought, the money, and whatever is still open.
    """
    buyer = await UserRepo(session).get_by_id(order.user_id)
    full = await OrderRepo(session).get_by_id(order.id) or order
    deliveries = await _deliveries(session, full)

    who = "—"
    avatar = None
    if buyer is not None:
        handle = f"@{escape_html(buyer.username)}" if buyer.username else "no username"
        who = f"{mention(buyer.discord_id)} · {handle} · <code>{buyer.discord_id}</code>"
        avatar = await bot.user_avatar_url(buyer.discord_id)

    status = order.status.value
    subtitle = _STATUS_LABEL.get(status, status)
    if order.placed_at:
        subtitle += f" · placed {timestamp(order.placed_at, 'R')}"
    lines = [
        thumbnail(avatar),
        f"<h2>{_STATUS_EMOJI.get(status, '•')} {order.order_number}</h2>",
        f"<small>{subtitle}</small>",
        "<hr>",
        f"👤 <b>Buyer</b> · {who}",
    ]
    if order.placed_at:
        lines.append(f"🕒 <b>Placed</b> · {timestamp(order.placed_at)}")
    if order.completed_at:
        lines.append(f"📬 <b>Delivered</b> · {timestamp(order.completed_at)}")
    if order.cancelled_at:
        lines.append(f"🚫 <b>Cancelled</b> · {timestamp(order.cancelled_at)}")
    if order.cancelled_by_admin_id:
        lines.append(f"👮 <b>By admin</b> · {mention(order.cancelled_by_admin_id)} · <code>{order.cancelled_by_admin_id}</code>")

    lines += ["<hr>", "<h3>🛍 Items</h3>"]
    for item in full.items:
        unit = format_minor(item.unit_price_minor, order.currency)
        line = f"• <b>{escape_html(item.product_name)}</b>"
        line += f" ×{item.qty} — {unit} each" if item.qty > 1 else f" — {unit}"
        lines.append(line)

        detail = []
        product = (
            await ProductRepo(session).get_by_id(item.product_id) if item.product_id else None
        )
        if product is not None:
            detail.append(
                "🤖 Auto" if product.fulfillment_mode is FulfillmentMode.AUTO else "✋ Manual"
            )
        if item.warranty_days:
            detail.append(f"🛡 {item.warranty_days}d warranty")
        delivery = deliveries.get(item.id)
        if delivery is not None and delivery.delivered_at:
            detail.append(f"📬 delivered {timestamp(delivery.delivered_at)}")
            if delivery.delivered_by_admin_id:
                detail.append(f"by <code>{delivery.delivered_by_admin_id}</code>")
        elif order.status is OrderStatus.PROCESSING:
            detail.append("⏳ awaiting fulfilment")
        if detail:
            lines.append(f"<small>{' · '.join(detail)}</small>")

    paid = "💎 Crypto (USDT, on chain)" if order.funding_source is FundingSource.CRYPTO else "💳 Wallet balance"
    lines += ["<hr>", f"💰 <b>Total</b> · <b>{format_minor(order.total_minor, order.currency)}</b>"]
    if order.discount_minor:
        lines.append(
            f"🏷 Subtotal {format_minor(order.subtotal_minor, order.currency)} "
            f"− discount {format_minor(order.discount_minor, order.currency)}"
        )
    lines.append(f"🧾 <b>Paid by</b> · {paid}")

    if order.crypto_payment_id:
        tx_hash = await _tx_hash(session, order.crypto_payment_id)
        if tx_hash:
            lines.append(f"🔗 <b>Tx</b> · <code>{escape_html(tx_hash)}</code>")

    if order.failure_reason:
        lines += ["<hr>", "<h3>🚫 Decline reason</h3>", escape_html(order.failure_reason)]

    if order.refund_state is not RefundState.NONE:
        amount = format_minor(order.refund_amount_minor or 0, order.currency)
        label = {
            RefundState.PARKED: f"🟠 <b>{amount}</b> parked in their Refund Wallet — not settled yet",
            RefundState.FROZEN: f"🧊 <b>{amount}</b> frozen in their Frozen Wallet — not released yet",
            RefundState.SETTLED: f"🟢 <b>{amount}</b> refunded and settled",
        }[order.refund_state]
        lines += ["<hr>", "<h3>💸 Refund</h3>", label]
        if order.refund_ticket_id:
            ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
            if ticket is not None:
                lines.append(f"🎫 Ticket: <code>{ticket.ticket_number}</code> ({ticket.status.value})")

    if order.status is OrderStatus.PROCESSING:
        lines += ["<hr>", "⏳ <b>Needs manual fulfilment</b> — use the buttons on the message below, or Admin panel → 🛒 Orders"]

    lines.append(f"<small>🆔 <code>{order.id}</code></small>")
    return "\n".join(line for line in lines if line)


async def _opening(session: AsyncSession, order: Order) -> str:
    """The first line of the order's forum post — what staff read in the post list, before opening
    the thread: what was bought, for how much, and how it was paid."""
    full = await OrderRepo(session).get_by_id(order.id) or order
    names = [
        f"{escape_html(item.product_name)}" + (f" ×{item.qty}" if item.qty > 1 else "")
        for item in full.items
    ]
    what = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
    paid = "💎 USDT" if order.funding_source is FundingSource.CRYPTO else "💳 Wallet"
    total = format_minor(order.total_minor, order.currency)
    return f"🛒 <b>{what or 'Order'}</b> · {total} · {paid}"


async def _tx_hash(session: AsyncSession, crypto_payment_id: int) -> str | None:
    from app.database.models.crypto import CryptoPayment

    payment = await session.get(CryptoPayment, crypto_payment_id)
    return payment.tx_hash if payment else None


def _event_line(event: OrderEvent, order: Order) -> str:
    head = _EVENT_LINE.get(event.kind, event.kind.value)
    facts = []
    if event.amount_minor:
        facts.append(f"💵 <b>{format_minor(event.amount_minor, event.currency or order.currency)}</b>")
    if event.actor_discord_id:
        who = "👤" if event.actor is OrderEventActor.USER else "👮"
        facts.append(f"{who} {mention(event.actor_discord_id)} · <code>{event.actor_discord_id}</code>")
    lines = [f"<h3>{head}</h3>"]
    if facts:
        lines.append(" · ".join(facts))
    if event.reason:
        lines.append(escape_html(event.reason))
    if event.reference:
        lines.append(f"<code>{escape_html(event.reference)}</code>")
    lines.append(f"<small><code>{event.event_number}</code> · {timestamp(event.created_at)}</small>")
    return "\n".join(lines)


async def _event_messages(session: AsyncSession, event: OrderEvent, order: Order) -> list[str]:
    """What one event posts into the thread — usually one card, more for a delivery.

    A Delivered post carries what the buyer was actually given, auto or by hand, so staff answering
    "I never got it" or "this login doesn't work" can see the exact credentials in the order's own
    thread instead of digging them out of the stock screens. Long deliveries are spread over as
    many messages as they need, since Discord cuts a long card off; a credential is never cut in two.
    """
    line = _event_line(event, order)
    if event.kind is not OrderEventKind.DELIVERED:
        return [line]

    from app.services.order_service import delivered_content

    full = await OrderRepo(session).get_by_id(order.id) or order
    content = [piece for item in full.items for piece in await delivered_content(session, item)]
    if not content:
        return [line]
    items = content if len(content) == 1 else [f"{i}. {c}" for i, c in enumerate(content, start=1)]
    return pack_messages(f"{line}<hr>🔑 <b>Delivered content</b>\n", items)


async def sync(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Open this order's topic if it has none, then post whatever history it hasn't posted yet.

    One call after any action on an order is enough — it works out what is missing by comparing
    `order_events` against the high-water mark, so callers never have to say what happened. That also
    makes it self-healing: a post that failed while the group was unreachable goes out on the next
    action instead of being lost.

    Entirely best-effort. An order is committed to the database and visible in the admin panel
    regardless, so a misconfigured group, a deleted topic or a revoked bot must never fail a purchase
    or a refund. Every failure is logged and swallowed.
    """
    group_id = get_settings().orders_channel_id
    if group_id is None:
        return

    try:
        if order.thread_id is None:
            who = await _buyer_handle(session, order)
            topic = await bot.create_forum_topic(
                chat_id=group_id, name=topic_name(order, who), opening=await _opening(session, order)
            )
            order.thread_id = topic.message_thread_id
            await session.flush()
            # The topic opens with the staff guide, so the commands are the first thing anyone new
            # sees. Its own try: a guide that fails to post must not cost the order its card.
            try:
                await bot.send_message(group_id, order_guide(), message_thread_id=order.thread_id)
            except BotAPIError as exc:
                logger.warning("Couldn't post the staff guide for %s (%s)", order.order_number, exc)
            card = await bot.send_message(
                group_id, await _order_card(bot, session, order), message_thread_id=order.thread_id
            )
            order.thread_card_message_id = card.message_id
            await session.flush()
        else:
            await _refresh_card(bot, session, group_id, order)

        events = await OrderEventRepo(session).list_for_order(order.id)
        pending = [e for e in events if e.id > (order.thread_last_event_id or 0)]
        for event in pending:
            for text in await _event_messages(session, event, order):
                await bot.send_message(group_id, text, message_thread_id=order.thread_id)
            # Advanced per event, not once at the end: if the fifth post fails, the four already sent
            # are not sent again next time.
            order.thread_last_event_id = event.id
            await session.flush()

        # A dead order's thread is closed so the group reads as a queue of live work. Two
        # exceptions, both of them unfinished business: money still held for the buyer — parked in
        # the Refund Wallet or frozen for review — and a dispute the buyer is connected to — that
        # thread is the conversation now, and closing it would mute both sides mid-sentence. Only an
        # admin's /close ends it.
        if await dispute_is_open(session, order):
            return
        money_held = order.refund_state in (RefundState.PARKED, RefundState.FROZEN)
        if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED) and not money_held:
            await _close(bot, group_id, order)
        elif order.status is OrderStatus.COMPLETED:
            # Delivered is done: the topic closes itself, which is the whole reason the group stays
            # readable as work rather than as history.
            await _close(bot, group_id, order)

    except BotAPIError as exc:
        logger.warning("Couldn't update the order thread for %s (%s)", order.order_number, exc)
    except Exception as exc:
        logger.error("Unexpected failure updating the order thread for %s: %s", order.order_number, exc)


async def _refresh_card(bot: Bot, session: AsyncSession, group_id: int, order: Order) -> None:
    """Edit the card at the top of the topic back into the truth.

    Swallows its own failures rather than letting them abort the sync: the event lines below are the
    part that must not be lost, and an unchanged card raises "message is not modified" as a matter of
    course.
    """
    if order.thread_card_message_id is None:
        return
    try:
        await bot.edit_message_text(
            await _order_card(bot, session, order),
            chat_id=group_id,
            message_id=order.thread_card_message_id,
            message_thread_id=order.thread_id,
        )
    except BotAPIError:
        pass


async def _close(bot: Bot, group_id: int, order: Order) -> None:
    try:
        await bot.close_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
    except BotAPIError:
        # Already closed, or the topic is gone. Neither is worth a warning — this is cosmetic.
        pass


async def reopen(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Bring a closed thread back before posting into it.

    A completed order's topic is closed, and Telegram refuses messages into a closed topic. Something
    can still happen afterwards — money parked on the order gets settled or moved — and that must land
    in the same thread rather than silently going nowhere.
    """
    group_id = get_settings().orders_channel_id
    if group_id is None or order.thread_id is None:
        return
    try:
        await bot.reopen_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
    except BotAPIError:
        pass
    await sync(bot, session, order)


async def dispute_is_open(session: AsyncSession, order: Order) -> bool:
    """Is this order's thread currently a live conversation with the buyer?

    Read off the ticket rather than off the order's status, because the two answer different
    questions: the order is cancelled either way, and what decides whether the thread stays open is
    whether anybody is still talking in it.
    """
    if order.refund_ticket_id is None:
        return False
    ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
    if ticket is None or ticket.order_id != order.id:
        return False
    return ticket.status in (TicketStatus.OPEN, TicketStatus.PENDING)
