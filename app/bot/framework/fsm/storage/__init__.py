"""FSM storage and context.

State is keyed by (chat, user), aiogram's default USER_IN_CHAT strategy: in a DM the chat is the user,
so a buyer's form follows them; in a staff channel each staff member has their own form, shared by
every thread of that channel — the fulfil/decline handlers check the thread themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.bot.framework.fsm.state import State


@dataclass(frozen=True)
class StorageKey:
    chat_id: int
    user_id: int

    def build(self, part: str) -> str:
        return f"fsm:{self.chat_id}:{self.user_id}:{part}"


class _KV(Protocol):
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: str, ex: int | None = None) -> Any: ...
    async def delete(self, *keys: str) -> Any: ...


def _state_name(state: State | str | None) -> str | None:
    if isinstance(state, State):
        return state.state
    return state


class RedisStorage:
    """FSM storage on any Redis-like client (`redis.asyncio.Redis` or `app.core.redis.MemoryRedis`).

    Survives restarts when backed by a real Redis, exactly as the Telegram bot's storage did.
    """

    def __init__(self, redis: _KV, *, state_ttl: int | None = None, data_ttl: int | None = None) -> None:
        self.redis = redis
        self._state_ttl = state_ttl
        self._data_ttl = data_ttl

    async def set_state(self, key: StorageKey, state: State | str | None = None) -> None:
        name = _state_name(state)
        if name is None:
            await self.redis.delete(key.build("state"))
        else:
            await self.redis.set(key.build("state"), name, ex=self._state_ttl)

    async def get_state(self, key: StorageKey) -> str | None:
        value = await self.redis.get(key.build("state"))
        if isinstance(value, bytes):
            value = value.decode()
        return value

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        if not data:
            await self.redis.delete(key.build("data"))
            return
        await self.redis.set(key.build("data"), json.dumps(_tag(data)), ex=self._data_ttl)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        raw = await self.redis.get(key.build("data"))
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode()
        return _revive(json.loads(raw))

    async def close(self) -> None:
        close = getattr(self.redis, "aclose", None)
        if close is not None:
            await close()


# FSM data is JSON, but a few handlers stash enum members (e.g. FulfillmentMode) and compare them
# with `is` afterwards. A `str` enum would silently serialise as its bare value and come back as a
# plain string, so `data["fulfillment_mode"] is FulfillmentMode.MANUAL` would quietly turn False.
# Enums are therefore tagged explicitly on the way in and revived on the way out.
_ENUM_TAG = "__enum__"


def _tag(value: Any) -> Any:
    import enum

    if isinstance(value, enum.Enum):
        cls = type(value)
        return {_ENUM_TAG: f"{cls.__module__}:{cls.__qualname__}", "value": value.value}
    if isinstance(value, dict):
        return {k: _tag(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_tag(v) for v in value]
    return value


def _revive(value: Any) -> Any:
    if isinstance(value, dict):
        if _ENUM_TAG in value and "value" in value and len(value) == 2:
            import importlib

            module_name, _, qualname = value[_ENUM_TAG].partition(":")
            try:
                obj: Any = importlib.import_module(module_name)
                for part in qualname.split("."):
                    obj = getattr(obj, part)
                return obj(value["value"])
            except Exception:
                return value["value"]
        return {k: _revive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_revive(v) for v in value]
    return value


class FSMContext:
    def __init__(self, storage: RedisStorage, key: StorageKey) -> None:
        self.storage = storage
        self.key = key

    async def set_state(self, state: State | str | None = None) -> None:
        await self.storage.set_state(self.key, state)

    async def get_state(self) -> str | None:
        return await self.storage.get_state(self.key)

    async def set_data(self, data: dict[str, Any]) -> None:
        await self.storage.set_data(self.key, data)

    async def get_data(self) -> dict[str, Any]:
        return await self.storage.get_data(self.key)

    async def get_value(self, key: str, default: Any = None) -> Any:
        return (await self.get_data()).get(key, default)

    async def update_data(self, data: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        current = await self.get_data()
        if data:
            current.update(data)
        current.update(kwargs)
        await self.set_data(current)
        return current

    async def clear(self) -> None:
        await self.set_state(None)
        await self.set_data({})
