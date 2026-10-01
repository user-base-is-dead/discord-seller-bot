from __future__ import annotations

import time
from typing import Any

from redis.asyncio import Redis


class MemoryRedis:
    """The handful of Redis commands the bot uses (FSM storage, flood limiting), kept in memory.

    Used when REDIS_URL is blank. State lives only as long as the process, which is fine for a single
    local instance; production should point REDIS_URL at a real Redis so forms survive a restart.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and deadline <= time.monotonic():
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return False
        return key in self._data

    async def get(self, key: str) -> Any:
        return self._data.get(key) if self._alive(key) else None

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._data[key] = value
        if ex:
            self._expiry[key] = time.monotonic() + ex
        else:
            self._expiry.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self._data.pop(key, None)
            self._expiry.pop(key, None)
        return removed

    async def incr(self, key: str) -> int:
        value = int(self._data.get(key, 0)) + 1 if self._alive(key) else 1
        self._data[key] = value
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        if not self._alive(key):
            return False
        self._expiry[key] = time.monotonic() + seconds
        return True

    async def aclose(self) -> None:
        self._data.clear()
        self._expiry.clear()


def build_redis(redis_url: str) -> Redis | MemoryRedis:
    if not redis_url.strip():
        return MemoryRedis()
    return Redis.from_url(redis_url, decode_responses=True)
