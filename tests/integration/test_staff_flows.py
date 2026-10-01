from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from tests.conftest import ADMIN_ID, ORDERS_CHANNEL, SUPPORT_CHANNEL
from tests.integration.test_shop_flow import _fund, _seed

STAFF_ID = 700000000000000001  # in the staff channels, not in ADMIN_IDS


def _only_thread(h, channel: int):
    threads = [t for t in h.bot.threads.values() if t.chat_id == channel]
    assert len(threads) == 1, threads
    return threads[0]


async def test_support_ticket_relay_and_close(harness):
    h = harness
    buyer = h.person(401, "dave", "Dave")
    staff = h.person(STAFF_ID, "helper")

    await h.command(buyer, "/support")
    menu = h.latest(buyer.id)
    assert "PowerX Support" in menu.markdown
    await h.press(buyer, "New Ticket")
    await h.press(buyer, "Payment or wallet")
    assert "Describe the problem" in h.latest(buyer.id).markdown
    await h.dm(buyer, "My USDT payment didn't arrive <tx 0xabc>")
    assert "is open" in h.bot.sent[-1].markdown

    thread = _only_thread(h, SUPPORT_CHANNEL)
    in_thread = h.bot.in_chat(SUPPORT_CHANNEL, thread.thread_id)
    assert "Staff guide" in in_thread[0].text
    assert "<mention>401</mention>" in in_thread[1].text and "<@401>" in in_thread[1].markdown
    assert "didn't arrive" in in_thread[2].markdown and "\\<tx 0xabc\\>" in in_thread[2].markdown

    # The buyer keeps talking in DMs: it lands in the thread.
    await h.dm(buyer, "Here is the **hash**: `0xdeadbeef`", files=[("receipt.png", b"PNG")])
    posted = h.bot.in_chat(SUPPORT_CHANNEL, thread.thread_id)
    assert posted[-2].kind == "file" and posted[-2].filename == "receipt.png"
    # Formatting preserved, under a small label naming the buyer.
    assert posted[-1].markdown == "-# 💬 @dave\nHere is the **hash**: `0xdeadbeef`"

    # A staff member (not an admin) answers in the thread: it reaches the buyer's DMs.
    await h.staff_says(staff, SUPPORT_CHANNEL, thread.thread_id, "Found it — crediting now ✅")
    reply = h.bot.in_chat(buyer.id)[-1]
    assert "PowerX Support" in reply.markdown and "crediting now" in reply.markdown

    # A second ticket is refused while one is open.
    await h.command(buyer, "/support")
    await h.press(buyer, "New Ticket")
    assert "already have an open ticket" in h.last_notice(buyer.id).text

    # My Tickets shows the conversation.
    await h.press(buyer, "My Tickets")
    ticket_button = next(label for label in h.latest(buyer.id).labels() if "TCK-" in label)
    await h.press(buyer, ticket_button)
    card = h.latest(buyer.id)
    assert "Support:" in card.markdown and "crediting now" in card.markdown

    # /close as a slash command inside the thread, by staff.
    replier = await h.command(staff, "/close", channel=SUPPORT_CHANNEL, thread_id=thread.thread_id)
    assert replier.acked
    assert any("closed" in s.text for s in h.bot.in_chat(SUPPORT_CHANNEL, thread.thread_id))
    assert thread.archived and thread.locked
    assert "now closed" in h.bot.in_chat(buyer.id)[-1].markdown


async def test_staff_command_in_thread_is_not_relayed(harness):
    h = harness
    buyer = h.person(402, "erin")
    staff = h.person(STAFF_ID, "helper")
    await h.command(buyer, "/support")
    await h.press(buyer, "New Ticket")
    await h.press(buyer, "General question")
    await h.dm(buyer, "hello")
    thread = _only_thread(h, SUPPORT_CHANNEL)
    before = len(h.bot.in_chat(buyer.id))
    await h.staff_says(staff, SUPPORT_CHANNEL, thread.thread_id, "/something internal")
    assert len(h.bot.in_chat(buyer.id)) == before


async def test_manual_order_fulfilled_by_staff_in_thread(harness):
    h = harness
    buyer = h.person(403, "frank")
    staff = h.person(STAFF_ID, "helper")
    await _seed(h, stock=[], mode=FulfillmentMode.MANUAL, name="Custom Logo")
    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 1_000)
    await h.press(buyer, "Shop")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Custom Logo")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")
    await h.press(buyer, "Pay from Wallet")
    await h.press(buyer, "Confirm")
    assert "Preparing your order" in h.latest(buyer.id).markdown

    thread = _only_thread(h, ORDERS_CHANNEL)
    prompt = next(s for s in h.bot.in_chat(ORDERS_CHANNEL, thread.thread_id) if "Manual order awaiting fulfilment" in s.text)
    assert not thread.archived

    await h.press(staff, "Fulfill now", on=prompt)
    ask = h.latest(ORDERS_CHANNEL, thread.thread_id)
    assert "Fulfil this order" in ask.markdown

    # A message in a different thread is not taken as the delivery.
    await h.staff_says(staff, ORDERS_CHANNEL, 123, "unrelated")
    await h.staff_says(staff, ORDERS_CHANNEL, thread.thread_id, "```\nLOGO-LINK: https://files.example/logo.zip\n```")
    delivery = h.bot.in_chat(buyer.id)[-1]
    assert "Your delivery" in delivery.markdown
    assert "```\nLOGO-LINK: https://files.example/logo.zip\n```" in delivery.markdown
    assert thread.archived and thread.locked  # delivered → closed, and nothing posted after it


async def test_manual_order_declined_to_refund_wallet(harness):
    h = harness
    buyer = h.person(404, "gina")
    staff = h.person(STAFF_ID, "helper")
    await _seed(h, stock=[], mode=FulfillmentMode.MANUAL, name="Custom Logo", price=700)
    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 1_000)
    await h.press(buyer, "Shop")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Custom Logo")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")
    await h.press(buyer, "Pay from Wallet")
    await h.press(buyer, "Confirm")

    thread = _only_thread(h, ORDERS_CHANNEL)
    prompt = next(s for s in h.bot.in_chat(ORDERS_CHANNEL, thread.thread_id) if "Manual order awaiting fulfilment" in s.text)
    await h.press(staff, "Decline", on=prompt)
    await h.staff_says(staff, ORDERS_CHANNEL, thread.thread_id, "Out of design slots this week")
    choose = h.latest(ORDERS_CHANNEL, thread.thread_id)
    assert "Where should" in choose.markdown
    await h.press(staff, "Refund Wallet", on=choose)
    receipt = choose  # the question is edited into the receipt, in place
    assert "Declined and Refunded" in receipt.markdown

    notice = h.bot.in_chat(buyer.id)[-1]
    assert "Order declined" in notice.markdown and "Out of design slots" in notice.markdown
    assert not thread.archived  # money parked → the thread stays open

    await h.command(buyer, "/refunds")
    assert "Refund Wallet: $7.00" in h.latest(buyer.id).markdown

    # Only an admin may settle; a staff member pressing the settle button is refused.
    await h.press(staff, "Settle refund now", on=receipt)
    assert "don't have access" in h.last_notice(staff.id).text

    admin = h.person(ADMIN_ID, "boss")
    await h.press(admin, "Settle refund now", on=receipt)
    assert "$7.00" in receipt.markdown and "Refund" in receipt.markdown
