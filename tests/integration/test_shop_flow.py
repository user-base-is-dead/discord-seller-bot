from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.models.wallet import TxnType
from app.services import catalog_service, wallet_service
from tests.conftest import ADMIN_ID, ORDERS_CHANNEL


async def _seed(h, *, stock: list[str], mode=FulfillmentMode.AUTO, price=500, warranty=7, name="Netflix Premium"):
    async with h.sessionmaker() as session:
        cat = await catalog_service.create_category(session, name="Streaming", emoji="🎬", description=None, image_file_id=None)
        pid = await catalog_service.create_product(
            session,
            category_id=cat,
            name=name,
            description="4K screen, private profile",
            price_minor=price,
            currency="USD",
            fulfillment_mode=mode,
            warranty_days=warranty,
            delivery_info="Change the profile PIN after login.",
            image_file_id=None,
        )
        if stock:
            await catalog_service.add_stock(session, product_id=pid, plaintext_payloads=stock, added_by_admin_id=ADMIN_ID)
        await session.commit()
    return cat, pid


async def _fund(h, discord_id: int, amount_minor: int):
    from app.database.repositories.user_repo import UserRepo

    async with h.sessionmaker() as session:
        user = await UserRepo(session).get_by_discord_id(discord_id)
        await wallet_service.credit(
            session,
            user_id=user.id,
            amount_minor=amount_minor,
            currency="USD",
            type_=TxnType.ADMIN_ADJUST,
            idempotency_key=f"test:{discord_id}:{amount_minor}",
        )
        await session.commit()


async def test_start_shows_home_menu(harness):
    h = harness
    buyer = h.person(111, "alice", "Alice")
    replier = await h.command(buyer, "/start")
    screen = h.latest(buyer.id)
    assert screen is not None and replier.acked
    assert "PowerX Digital" in screen.markdown and "Alice" in screen.markdown
    assert {"🛒 Shop", "👛 Wallet", "🧾 My Orders", "🛡️ Warranty", "🎧 Support"} <= set(screen.labels())
    # Not an admin: no admin row.
    assert not any("Admin" in label for label in screen.labels())


async def test_admin_sees_admin_panel_button(harness):
    h = harness
    admin = h.person(ADMIN_ID, "boss")
    await h.command(admin, "/start")
    screen = h.latest(admin.id)
    assert any("Admin Panel" in label for label in screen.labels())
    await h.press(admin, "Admin Panel")
    assert "Admin Panel" in h.latest(admin.id).markdown


async def test_browse_buy_with_wallet_and_history(harness):
    h = harness
    buyer = h.person(222, "bob", "Bob")
    _cat, _pid = await _seed(h, stock=["login1@mail.com:pass1", "login2@mail.com:pass2"])

    await h.command(buyer, "/start")
    await _fund(h, buyer.id, 2_000)

    await h.press(buyer, "Shop")
    assert "PowerX Shop" in h.latest(buyer.id).markdown
    await h.press(buyer, "Streaming")
    assert "Netflix Premium" in h.latest(buyer.id).markdown
    await h.press(buyer, "Netflix Premium")
    detail = h.latest(buyer.id)
    assert "4K screen" in detail.markdown and "$5.00" not in detail.labels()
    await h.press(buyer, "Buy Now")
    assert "Choose quantity" in h.latest(buyer.id).markdown

    # The quantity is typed, as on Telegram.
    await h.dm(buyer, "2")
    choice = h.latest(buyer.id)
    assert "How would you like to pay" in choice.markdown and "Quantity" in choice.markdown
    await h.press(buyer, "Pay from Wallet", on=choice)
    confirm = h.latest(buyer.id)
    assert "Review your order" in confirm.markdown
    await h.press(buyer, "Confirm", on=confirm)

    delivered = h.latest(buyer.id)
    assert "confirmed" in delivered.markdown
    assert "login1@mail.com" in delivered.markdown and "login2@mail.com" in delivered.markdown
    assert "Change the profile PIN" in delivered.markdown

    # Every order gets a thread in the orders channel, with the staff guide and the card.
    threads = [t for t in h.bot.threads.values() if t.chat_id == ORDERS_CHANNEL]
    assert len(threads) == 1
    posted = h.texts(ORDERS_CHANNEL, threads[0].thread_id)
    assert any("Staff guide" in t for t in posted) and any("ORD-" in t for t in posted)
    assert threads[0].archived  # a delivered order's thread closes itself

    await h.command(buyer, "/orders")
    history = h.latest(buyer.id)
    assert "My Orders" in history.markdown
    order_button = next(label for label in history.labels() if "ORD-" in label)
    await h.press(buyer, order_button, on=history)
    assert "Total" in h.latest(buyer.id).markdown


async def test_insufficient_balance_is_an_alert(harness):
    h = harness
    buyer = h.person(333, "carol")
    await _seed(h, stock=["k1"], price=10_000)
    await h.command(buyer, "/products")
    await h.press(buyer, "Streaming")
    await h.press(buyer, "Netflix")
    await h.press(buyer, "Buy Now")
    await h.press(buyer, "Buy 1")
    await h.press(buyer, "Wallet")
    await h.press(buyer, "Confirm")
    notice = h.last_notice(buyer.id)
    assert notice is not None and notice.alert and "balance" in notice.text.lower()
