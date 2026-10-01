from __future__ import annotations

from sqlalchemy import select

from app.core.security import get_cipher
from app.database.models.broadcast import Broadcast, BroadcastStatus
from app.database.models.catalog import Product, StockItem
from tests.conftest import ADMIN_ID
from tests.integration.test_shop_flow import _seed


async def test_product_wizard_with_split_stock(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    await _seed(h, stock=[])  # a category to file the product in

    await h.command(admin, "/admin")
    await h.press(admin, "Products")
    await h.press(admin, "Add Product")
    assert "step 1 of" in h.latest(admin.id).markdown
    await h.press(admin, "Streaming")
    await h.dm(admin, "Spotify Family *Duo*")
    await h.press(admin, "Skip")
    await h.dm(admin, "3.50")
    await h.press(admin, "Auto")
    await h.press(admin, "30d")
    await h.press(admin, "Skip")
    await h.dm(admin, "one@mail.com:pw1\ntwo@mail.com:pw_2\nthree@mail.com:pw*3")
    question = h.latest(admin.id)
    assert "3 lines" in question.markdown
    await h.press(admin, "One per line")
    await h.press(admin, "Auto (count credentials)")
    assert any("created" in s.markdown for s in h.bot.in_chat(admin.id))
    prompt = h.latest(admin.id)
    assert "new product" in prompt.markdown
    await h.press(admin, "Decline", on=prompt)

    async with h.sessionmaker() as session:
        product = (await session.execute(select(Product).where(Product.name == "Spotify Family *Duo*"))).scalar_one()
        items = (await session.execute(select(StockItem).where(StockItem.product_id == product.id))).scalars().all()
    assert product.price_minor == 350 and product.warranty_days == 30
    payloads = sorted(get_cipher().decrypt(i.payload) for i in items)
    # Each piece keeps the admin's text byte for byte, marked as markdown to pass through.
    assert payloads == ["<md>one@mail.com:pw1</md>", "<md>three@mail.com:pw*3</md>", "<md>two@mail.com:pw_2</md>"]

    # The product name is shown literally, not as markdown.
    await h.command(admin, "/products")
    await h.press(admin, "Streaming")
    assert "Spotify Family \\*Duo\\*" in h.latest(admin.id).markdown


async def test_buyer_receives_markdown_stock_verbatim(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    buyer = h.person(601, "olga")
    _cat, pid = await _seed(h, stock=[])
    await h.command(admin, "/admin")
    await h.press(admin, "Products")
    await h.press(admin, "Streaming")
    await h.press(admin, "Netflix")
    await h.press(admin, "Add Stock")
    await h.dm(admin, "```\nuser: a@b.c\npass: p_w*d\n```")
    await h.press(admin, "Keep as 1 item")

    from tests.integration.test_shop_flow import _fund

    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 1_000)
    await h.command(buyer, "/products")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Netflix")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")
    await h.press(buyer, "Pay from Wallet")
    await h.press(buyer, "Confirm")
    assert "```\nuser: a@b.c\npass: p_w*d\n```" in h.latest(buyer.id).markdown


async def test_csv_export_and_import(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    await _seed(h, stock=["x"])
    await h.command(admin, "/admin")
    await h.press(admin, "Products")
    await h.press(admin, "Export CSV")
    exported = [s for s in h.bot.in_chat(admin.id) if s.kind == "file"][-1]
    assert exported.filename == "products.csv" and b"Netflix Premium" in exported.data

    await h.press(admin, "Import CSV", on=h.latest(admin.id))
    csv_text = "name,price,category\nYouTube Premium,2.99,Streaming\n"
    await h.dm(admin, files=[("new.csv", csv_text.encode())])
    assert "Import complete" in h.bot.sent[-1].markdown
    async with h.sessionmaker() as session:
        names = {p.name for p in (await session.execute(select(Product))).scalars()}
    assert "YouTube Premium" in names


async def test_broadcast_compose_preview_send(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    buyers = [h.person(610 + i, f"b{i}") for i in range(3)]
    await _seed(h, stock=["a", "b"])
    for buyer in buyers:
        await h.command(buyer, "/start")

    await h.command(admin, "/admin")
    await h.press(admin, "Broadcast")
    await h.dm(admin, "🔥 **Big sale** today only!")
    await h.dm(admin, "look", files=[("banner.png", b"PNG")])
    await h.press(admin, "Done")
    copies = [s for s in h.bot.in_chat(admin.id) if s.kind == "copy"]
    assert len(copies) == 2  # the preview is the real thing
    await h.press(admin, "Attach product")
    await h.press(admin, "Streaming")
    await h.press(admin, "Netflix Premium")
    await h.press(admin, "Send")
    await h.settle()

    async with h.sessionmaker() as session:
        broadcast = (await session.execute(select(Broadcast))).scalar_one()
    assert broadcast.status is BroadcastStatus.COMPLETED and broadcast.sent_count == 4  # 3 buyers + admin
    for buyer in buyers:
        received = [s for s in h.bot.in_chat(buyer.id) if s.kind == "copy"]
        assert [r.text for r in received] == ["🔥 **Big sale** today only!", "look"]
        assert received[-1].labels() == ["🛒 Buy Now"] and received[0].markup is None

    # Buy Now under a copied (non-editable) post opens the checkout as a new message.
    last = [s for s in h.bot.in_chat(buyers[0].id) if s.kind == "copy"][-1]
    await h.press(buyers[0], "Buy Now", on=last)
    assert "Choose quantity" in h.latest(buyers[0].id).markdown

    await h.command(admin, f"/broadcast_status {broadcast.id}")
    assert "Sent: 4" in h.bot.sent[-1].markdown


async def test_admin_tools_render(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    buyer = h.person(620, "pete")
    await _seed(h, stock=["a"])
    await h.command(buyer, "/start")

    await h.command(admin, f"/adjust_balance {buyer.id} +12.50 goodwill")
    assert "New balance: $12.50" in h.bot.sent[-1].markdown

    for command, expected in [
        ("/dashboard", "Dashboard"),
        ("/pending_orders", "PENDING FULFILLMENT"),
        ("/refund_wallets", "Refund"),
        ("/wallet_balances", "$12.50"),
        ("/open_tickets", "OPEN TICKETS"),
    ]:
        await h.command(admin, command)
        assert expected.lower() in h.bot.sent[-1].markdown.lower(), command

    await h.command(admin, "/admin")
    for label in ["Dashboard", "Logs", "Categories", "Orders", "Users", "Refund Wallets", "Wallet Balances"]:
        await h.command(admin, "/admin")
        await h.press(admin, label)
        assert h.latest(admin.id).markdown


async def test_staff_refunds_a_warranty_claim(harness):
    from app.database.models.order import Warranty
    from tests.conftest import SUPPORT_CHANNEL
    from tests.integration.test_shop_flow import _fund
    from tests.integration.test_staff_flows import STAFF_ID, _only_thread

    h = harness
    buyer = h.person(630, "quinn")
    staff = h.person(STAFF_ID, "helper")
    await _seed(h, stock=["acc"], warranty=30, price=900)
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
        warranty = (await session.execute(select(Warranty))).scalar_one()

    await h.staff_says(staff, SUPPORT_CHANNEL, thread.thread_id, f"/refund {warranty.id} Account got banned")
    assert "Warranty claim refunded" in h.bot.in_chat(buyer.id)[-1].markdown
    assert not thread.archived  # stays open until the money is settled
    await h.command(buyer, "/refunds")
    assert "$9.00" in h.latest(buyer.id).markdown
