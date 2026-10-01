from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import discord

from app.bot.commands import ALL_COMMANDS
from app.bot.framework import Dispatcher
from app.bot.framework.client import StoreClient
from app.bot.framework.fsm.storage.redis import RedisStorage
from app.bot.handlers.admin.balance_adjust import router as admin_balance_adjust_router
from app.bot.handlers.admin.broadcast import router as admin_broadcast_router
from app.bot.handlers.admin.categories import router as admin_categories_router
from app.bot.handlers.admin.dashboard import router as admin_dashboard_router
from app.bot.handlers.admin.guard import router as admin_guard_router
from app.bot.handlers.admin.logs import router as admin_logs_router
from app.bot.handlers.admin.orders import router as admin_orders_router
from app.bot.handlers.admin.orders import staff_router as admin_orders_staff_router
from app.bot.handlers.admin.panel import router as admin_panel_router
from app.bot.handlers.admin.payments import router as admin_payments_router
from app.bot.handlers.admin.products import router as admin_products_router
from app.bot.handlers.admin.refund_wallets import router as admin_refund_wallets_router
from app.bot.handlers.admin.stock_items import router as admin_stock_items_router
from app.bot.handlers.admin.support import router as admin_support_router
from app.bot.handlers.admin.support import staff_router as admin_support_staff_router
from app.bot.handlers.admin.users import router as admin_users_router
from app.bot.handlers.admin.warranty_claims import router as admin_warranty_claims_router
from app.bot.handlers.nav import router as nav_router
from app.bot.handlers.orders.checkout import router as orders_checkout_router
from app.bot.handlers.orders.history import router as orders_history_router
from app.bot.handlers.payments.topup_crypto import router as payments_topup_crypto_router
from app.bot.handlers.products.browse import router as products_browse_router
from app.bot.handlers.support.create import router as support_create_router
from app.bot.handlers.support.my_tickets import router as support_my_tickets_router
from app.bot.handlers.support.relay import router as support_relay_router
from app.bot.handlers.user.help import router as help_router
from app.bot.handlers.user.language import router as language_router
from app.bot.handlers.user.menu_placeholders import router as menu_placeholders_router
from app.bot.handlers.user.profile import router as profile_router
from app.bot.handlers.user.refunds import router as refunds_router
from app.bot.handlers.user.start import router as start_router
from app.bot.handlers.user.wallet_history import router as wallet_history_router
from app.bot.handlers.warranty.claim import router as warranty_claim_router
from app.bot.handlers.warranty.screen import router as warranty_router
from app.bot.middlewares.ban_check import BanCheckMiddleware
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.error import ErrorMiddleware
from app.bot.middlewares.pending_payment import PendingPaymentMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.bot.storefront import register_storefront
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import build_redis
from app.database.repositories.admin_repo import AdminRepo
from app.database.repositories.audit_repo import AuditRepo
from app.database.schema_guard import check_schema
from app.database.session import build_engine, build_sessionmaker, session_scope
from app.jobs.broadcast_worker import resume_interrupted_broadcasts
from app.jobs.scheduler import build_scheduler

logger = logging.getLogger(__name__)


def include_routers(dp: Dispatcher) -> None:
    # Same order as the Telegram bot: the first matching handler wins, so the order is the policy.
    dp.include_router(start_router)
    dp.include_router(help_router)
    # Not registered on the Telegram bot, so its advertised /language command fell through to the
    # support relay and did nothing. Registered here so the command it lists works.
    dp.include_router(language_router)
    dp.include_router(admin_panel_router)
    dp.include_router(admin_categories_router)
    dp.include_router(admin_products_router)
    dp.include_router(admin_stock_items_router)
    dp.include_router(admin_orders_router)
    # Admin-or-staff-channel routers (see app/bot/filters/staff.py): the parts of the admin surface
    # that staff use inside the support and orders channels. Before the guard like every admin
    # router, so a staff member doing that work is served instead of refused.
    dp.include_router(admin_orders_staff_router)
    dp.include_router(admin_refund_wallets_router)
    dp.include_router(admin_payments_router)
    dp.include_router(admin_balance_adjust_router)
    dp.include_router(admin_support_router)
    dp.include_router(admin_support_staff_router)
    dp.include_router(admin_dashboard_router)
    dp.include_router(admin_users_router)
    dp.include_router(admin_logs_router)
    dp.include_router(admin_broadcast_router)
    dp.include_router(admin_warranty_claims_router)
    # Must sit after every admin router (admins are already served above) and before the
    # generic nav/catch-all routers, so a non-admin gets an explicit "not authorized" instead
    # of silence or a misleading "unknown action".
    dp.include_router(admin_guard_router)
    dp.include_router(products_browse_router)
    dp.include_router(orders_checkout_router)
    dp.include_router(orders_history_router)
    dp.include_router(payments_topup_crypto_router)
    dp.include_router(profile_router)
    dp.include_router(refunds_router)
    dp.include_router(wallet_history_router)
    dp.include_router(warranty_router)
    dp.include_router(warranty_claim_router)
    dp.include_router(support_create_router)
    dp.include_router(support_my_tickets_router)
    dp.include_router(nav_router)
    dp.include_router(menu_placeholders_router)
    dp.include_router(support_relay_router)  # catch-all: must stay last


def setup_dispatcher(dp: Dispatcher, bot, redis, sessionmaker, settings) -> Dispatcher:
    """The update pipeline, wired exactly as on Telegram (outermost first)."""
    # withErrorBoundary -> withRateLimit -> withUser -> withAuth(per-handler) -> handler
    dp.update.middleware(ErrorMiddleware(bot, settings.log_channel_id))
    dp.update.middleware(ThrottlingMiddleware(redis))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.update.middleware(UserMiddleware(default_locale=settings.default_locale))
    dp.update.middleware(BanCheckMiddleware())
    # After the user is known: any interaction other than paying cancels an open USDT checkout.
    dp.update.middleware(PendingPaymentMiddleware())
    include_routers(dp)
    return dp


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)

    # A schema behind the code is not a per-screen bug, it is a broken deployment: the store, the
    # orders list, the profile — every screen whose SELECT names a column the database does not
    # have — answers "Something went wrong on our end" and the reason lives only in the log. So it
    # is checked once, here, and refused loudly rather than served broken.
    report = await check_schema(engine)
    if report.is_broken:
        logger.error(
            "Database schema is behind the code - refusing to start.\n%s\n"
            "Fix: run `python -m scripts.bootstrap_db` (or `alembic upgrade head`) against this "
            "database, then start the bot again. Run `python -m scripts.doctor` for the full report.",
            report.describe(),
        )
        await engine.dispose()
        raise SystemExit(1)
    if report.revision_behind:
        logger.warning("Pending migrations: %s", report.describe())

    async with session_scope(sessionmaker) as session:
        revoked = await AdminRepo(session).sync_env_owners(settings.admin_ids)
        if revoked:
            logger.warning("Revoked admin access for ids no longer in ADMIN_IDS: %s", revoked)
            await AuditRepo(session).log(
                actor_discord_id=0,
                action="security.admin_revoked_on_boot",
                target_type="env",
                target_id=",".join(str(i) for i in revoked)[:64],
                metadata={"revoked": revoked, "admin_ids": settings.admin_ids},
            )

    if not settings.admin_ids:
        logger.warning("ADMIN_IDS is empty — nobody can open the admin panel.")
    if settings.support_channel_id is None:
        logger.warning("SUPPORT_CHANNEL_ID is unset — support tickets cannot reach staff.")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", settings.wallet_address.strip()):
        logger.warning("WALLET_ADDRESS is not a BEP-20 address (0x + 40 hex) — USDT checkout can't be paid until it is set.")

    redis = build_redis(settings.redis_url)
    if not settings.redis_url.strip():
        logger.warning("REDIS_URL is unset — forms and flood limits are kept in memory (lost on restart).")
    storage = RedisStorage(redis=redis)

    # The client needs the dispatcher and the dispatcher's error middleware needs the client's Bot;
    # middlewares only run at dispatch time, so they are attached right after the client exists.
    dp = Dispatcher(storage=storage)
    client = StoreClient(
        dispatcher=dp,
        staff_channel_ids=(settings.support_channel_id, settings.orders_channel_id),
        known_channel_ids=(settings.log_channel_id,),
        commands=ALL_COMMANDS,
        accent_colour=settings.accent_colour,
        button_width=settings.button_width,
        sync_commands=settings.sync_commands,
    )
    bot = client.bot
    setup_dispatcher(dp, bot, redis, sessionmaker, settings)
    register_storefront(client, sessionmaker)

    scheduler = build_scheduler(sessionmaker, bot)
    background: set[asyncio.Task] = set()

    async def on_first_ready() -> None:
        # Jobs DM buyers and post into staff threads, so they start once the gateway is ready.
        scheduler.start()
        # Resumes any broadcast left RUNNING by a previous process (crash/restart) — PENDING
        # deliveries pick up exactly where they left off. In the background: a big broadcast must
        # not hold the rest of the start-up.
        task = asyncio.create_task(resume_interrupted_broadcasts(bot, settings.database_url, sessionmaker))
        background.add(task)
        task.add_done_callback(background.discard)

    client.on_first_ready(on_first_ready)

    logger.info("Starting Discord bot (environment=%s)", settings.environment)
    try:
        async with client:
            await client.start(settings.bot_token)
    except discord.LoginFailure:
        logger.error(
            "Discord rejected DISCORD_TOKEN. Copy the bot token from the Developer Portal "
            "(Applications → your app → Bot → Reset Token) into .env and start again."
        )
        raise SystemExit(1) from None
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Discord refused the Message Content intent. Enable it in the Developer Portal "
            "(Applications → your app → Bot → Privileged Gateway Intents → Message Content Intent) — "
            "without it the bot cannot read what staff type in ticket and order threads."
        )
        raise SystemExit(1) from None
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        for task in background:
            task.cancel()
        with contextlib.suppress(Exception):
            await storage.close()
        await engine.dispose()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
