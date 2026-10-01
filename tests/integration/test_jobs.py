from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.database.models.order import Warranty
from app.database.models.support import SupportTicket, TicketMessage, TicketStatus
from tests.conftest import SUPPORT_CHANNEL
from tests.integration.test_shop_flow import _fund, _seed
from tests.integration.test_staff_flows import _only_thread


async def test_idle_ticket_is_archived_and_buyer_told(harness):
    from app.jobs.ticket_archival import archive_stale_tickets

    h = harness
    buyer = h.person(801, "rita")
    await h.command(buyer, "/support")
    await h.press(buyer, "New Ticket")
    await h.press(buyer, "General question")
    await h.dm(buyer, "anyone there?")
    thread = _only_thread(h, SUPPORT_CHANNEL)

    long_ago = datetime.now(UTC) - timedelta(hours=30)
    async with h.sessionmaker() as session:
        await session.execute(update(TicketMessage).values(created_at=long_ago))
        await session.execute(update(SupportTicket).values(opened_at=long_ago))
        await session.commit()

    assert await archive_stale_tickets(h.sessionmaker, h.bot) == 1
    async with h.sessionmaker() as session:
        ticket = (await session.execute(select(SupportTicket))).scalar_one()
    assert ticket.status is TicketStatus.CLOSED
    assert "now closed" in h.bot.in_chat(buyer.id)[-1].markdown
    assert thread.archived and thread.locked


async def test_unanswered_warranty_claim_auto_rejects(harness):
    from app.jobs.warranty_auto_reject import auto_reject_expired_warranty_claims

    h = harness
    buyer = h.person(802, "sam")
    await _seed(h, stock=["acc"], warranty=30)
    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 1_000)
    await h.command(buyer, "/products")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Netflix")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")
    await h.press(buyer, "Pay from Wallet")
    await h.press(buyer, "Confirm")
    await h.command(buyer, "/warranty")
    await h.press(buyer, "Claim #1")
    thread = _only_thread(h, SUPPORT_CHANNEL)

    async with h.sessionmaker() as session:
        await session.execute(update(Warranty).values(claim_deadline_at=datetime.now(UTC) - timedelta(minutes=1)))
        await session.commit()

    assert await auto_reject_expired_warranty_claims(h.sessionmaker, h.bot) == 1
    assert "closed automatically" in h.bot.in_chat(buyer.id)[-1].markdown
    assert thread.archived and thread.locked
