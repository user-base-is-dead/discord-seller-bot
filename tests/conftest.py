from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ADMIN_ID = 900000000000000001
SUPPORT_CHANNEL = 800000000000000001
ORDERS_CHANNEL = 800000000000000002

os.environ.update(
    {
        "DISCORD_TOKEN": "test-token",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "WALLET_ADDRESS": "0x1111111111111111111111111111111111111111",
        "ADMIN_IDS": str(ADMIN_ID),
        "SUPPORT_CHANNEL_ID": str(SUPPORT_CHANNEL),
        "ORDERS_CHANNEL_ID": str(ORDERS_CHANNEL),
        "REDIS_URL": "",
        "COMMUNITY_CHANNEL_URL": "",
        "COMMUNITY_GROUP_URL": "",
        "DATABASE_URL": "sqlite+aiosqlite:///./_pytest_bootstrap.db",
    }
)


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    from app.bot.framework.fsm.storage import RedisStorage
    from app.bot.framework.router import Dispatcher
    from app.core import config
    from app.core.redis import MemoryRedis
    from app.database import models  # noqa: F401 - registers every table
    from app.database.base import Base
    from app.database.session import build_engine, build_sessionmaker
    from app.main import setup_dispatcher
    from app.services import broadcast_service
    from tests.harness import FakeBot, Harness

    monkeypatch.setattr(broadcast_service, "SEND_DELAY_SECONDS", 0)
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'store.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    config.get_settings.cache_clear()
    settings = config.get_settings()

    engine = build_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)

    redis = MemoryRedis()
    dp = Dispatcher(storage=RedisStorage(redis))
    bot = FakeBot({SUPPORT_CHANNEL, ORDERS_CHANNEL})
    setup_dispatcher(dp, bot, redis, sessionmaker, settings)
    h = Harness(dp, bot, sessionmaker)
    try:
        yield h
    finally:
        await h.settle()
        # Routers are module-level singletons; release them for the next test's dispatcher.
        for router in list(dp.sub_routers):
            router.parent_router = None
        dp.sub_routers.clear()
        await engine.dispose()
        config.get_settings.cache_clear()
