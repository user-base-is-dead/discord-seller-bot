from __future__ import annotations

import time

from sqlalchemy import select

from app.database.models.crypto import CryptoPayment
from app.database.models.order import Warranty, WarrantyStatus
from app.database.models.user import User as UserRow
from app.database.models.user import UserStatus
from tests.conftest import ADMIN_ID, SUPPORT_CHANNEL
from tests.integration.test_shop_flow import _fund, _seed
from tests.integration.test_staff_flows import STAFF_ID, _only_thread


async def _buy_one(h, buyer):
    await h.command(buyer, "/products")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Netflix")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")


async def test_warranty_claim_resolved_with_done(harness):
    h = harness
    buyer = h.person(501, "hank")
    staff = h.person(STAFF_ID, "helper")
    await _seed(h, stock=["acc-1"], warranty=30)
    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 1_000)
    await _buy_one(h, buyer)
    await h.press(buyer, "Pay from Wallet")
    await h.press(buyer, "Confirm")

    await h.command(buyer, "/warranty")
    screen = h.latest(buyer.id)
    assert "Warranty" in screen.markdown and "Active" in screen.markdown
    await h.press(buyer, "Claim #1")
    assert "Claim submitted" in h.latest(buyer.id).markdown

    thread = _only_thread(h, SUPPORT_CHANNEL)
    posted = h.bot.in_chat(SUPPORT_CHANNEL, thread.thread_id)
    assert "Warranty claim" in posted[0].markdown  # the claim guide opens the thread
    async with h.sessionmaker() as session:
        warranty = (await session.execute(select(Warranty))).scalar_one()
    assert warranty.status is WarrantyStatus.CLAIMED

    # Settled from the thread with a slash command, by a staff member.
    await h.command(staff, f"/done {warranty.id}", channel=SUPPORT_CHANNEL, thread_id=thread.thread_id)
    assert any("resolved" in s.text for s in h.bot.in_chat(SUPPORT_CHANNEL, thread.thread_id))
    assert "Warranty claim resolved" in h.bot.in_chat(buyer.id)[-1].markdown
    assert thread.archived and thread.locked
    async with h.sessionmaker() as session:
        warranty = await session.get(Warranty, warranty.id)
    assert warranty.status is WarrantyStatus.ACTIVE


async def test_usdt_invoice_check_and_leave(harness):
    h = harness
    buyer = h.person(502, "iris")
    await _seed(h, stock=["acc-1"], price=500)
    await h.command(buyer, "/start")
    await _buy_one(h, buyer)
    await h.press(buyer, "Pay with USDT")
    invoice = h.latest(buyer.id)
    assert "USDT Payment" in invoice.markdown and "BNB Smart Chain" in invoice.markdown

    # Copy buttons hand over the exact value; they never leave the payment screen.
    await h.press(buyer, "Copy amount", on=invoice)
    amount = h.last_notice(buyer.id).text
    assert amount.startswith("5.2")

    await h.press(buyer, "Check Payment", on=invoice)
    assert "Not received yet" in h.last_notice(buyer.id).text

    # Anything else cancels the invoice and gives the item back.
    await h.press(buyer, "Menu", on=invoice)
    assert "USDT payment cancelled" in h.bot.in_chat(buyer.id)[-1].markdown
    async with h.sessionmaker() as session:
        payment = (await session.execute(select(CryptoPayment))).scalar_one()
    assert payment.status == "CANCELLED"


async def test_usdt_paid_in_time_places_the_order(harness, monkeypatch):
    from app.jobs import crypto_payment_checker
    from app.services.payments import blockchain_monitor

    h = harness
    buyer = h.person(503, "jack")
    await _seed(h, stock=["acc-XYZ"], price=500)
    await h.command(buyer, "/start")
    await _buy_one(h, buyer)
    await h.press(buyer, "Pay with USDT")
    async with h.sessionmaker() as session:
        payment = (await session.execute(select(CryptoPayment))).scalar_one()

    async def transfers(self, limit: int = 25):
        return [{"hash": "0xabc", "value": float(payment.expected_amount), "from": "0xbuyer", "timestamp": time.time()}]

    monkeypatch.setattr(blockchain_monitor.BlockchainMonitor, "fetch_recent_transfers", transfers)
    await crypto_payment_checker.check_crypto_payments(h.sessionmaker, h.bot)

    delivered = h.bot.in_chat(buyer.id)[-1]
    assert "Payment received" in delivered.markdown and "acc-XYZ" in delivered.markdown


async def test_banned_user_keeps_support_only(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    buyer = h.person(504, "kate")
    await h.command(buyer, "/start")

    await h.command(admin, "/admin")
    await h.press(admin, "Users")
    await h.press(admin, "Search")
    await h.dm(admin, "kate")
    assert "kate" in h.latest(admin.id).markdown
    await h.press(admin, "Ban")
    async with h.sessionmaker() as session:
        row = (await session.execute(select(UserRow).where(UserRow.discord_id == buyer.id))).scalar_one()
    assert row.status is UserStatus.BANNED

    await h.command(buyer, "/wallet")
    assert "suspended" in h.bot.sent[-1].markdown
    await h.command(buyer, "/support")
    assert "PowerX Support" in h.latest(buyer.id).markdown


async def test_admin_surfaces_refuse_non_admins(harness):
    h = harness
    buyer = h.person(505, "leo")
    await h.command(buyer, "/admin")
    assert "don't have access" in h.bot.sent[-1].markdown

    # A forged admin button (custom_id typed by hand) is refused with an alert, and audited.
    await h.command(buyer, "/start")
    await h.click(buyer, "amisc:users::1", on=h.latest(buyer.id))
    assert "don't have access" in h.last_notice(buyer.id).text
    from app.database.models.audit import AuditLog

    async with h.sessionmaker() as session:
        actions = [a.action for a in (await session.execute(select(AuditLog))).scalars()]
    assert actions.count("security.unauthorized_admin") == 2


async def test_flood_limit(harness):
    h = harness
    buyer = h.person(506, "mia")
    for _ in range(22):
        await h.dm(buyer, "hi")
    assert sum("lot of taps" in s.markdown for s in h.bot.in_chat(buyer.id)) == 1


async def test_language_picker_and_help(harness):
    h = harness
    buyer = h.person(507, "nina")
    await h.command(buyer, "/language")
    await h.press(buyer, "English")
    assert "PowerX Digital" in h.latest(buyer.id).markdown
    assert "Language set" in h.last_notice(buyer.id).text
    await h.command(buyer, "/help")
    assert "/products" in h.latest(buyer.id).markdown
