"""Routers and the dispatcher, with aiogram's propagation rules.

  * routers are tried in inclusion order; inside a router, handlers in registration order;
  * router-level filters (`router.message.filter(IsAdmin())`) gate every handler of that observer;
  * the first handler whose filters all pass runs, and propagation stops there — whatever it
    returns — unless it raises SkipHandler;
  * update middlewares (`dp.update.middleware(...)`) wrap the whole propagation, outermost first.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from app.bot.framework._inject import call_with_injection
from app.bot.framework.filters import evaluate_filter
from app.bot.framework.fsm.storage import FSMContext, RedisStorage, StorageKey
from app.bot.framework.types import Update

logger = logging.getLogger(__name__)


class SkipHandler(Exception):
    """Raised by a handler to let the next matching handler take the event."""


class CancelHandler(Exception):
    pass


class _Unhandled:
    def __repr__(self) -> str:
        return "UNHANDLED"


UNHANDLED = _Unhandled()

Handler = Callable[..., Awaitable[Any]]


class HandlerObject:
    __slots__ = ("callback", "filters")

    def __init__(self, callback: Handler, filters: tuple[Any, ...]) -> None:
        self.callback = callback
        self.filters = filters

    async def check(self, event: Any, data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        extra: dict[str, Any] = {}
        for flt in self.filters:
            result = await evaluate_filter(flt, event, {**data, **extra})
            if not result:
                return False, {}
            if isinstance(result, dict):
                extra.update(result)
        return True, extra

    async def call(self, event: Any, data: dict[str, Any]) -> Any:
        return await call_with_injection(self.callback, event, data)


class EventObserver:
    def __init__(self, event_name: str) -> None:
        self.event_name = event_name
        self.handlers: list[HandlerObject] = []
        self._root = HandlerObject(_noop, ())
        self.middlewares: list[Any] = []

    def register(self, callback: Handler, *filters: Any, **_: Any) -> Handler:
        self.handlers.append(HandlerObject(callback, filters))
        return callback

    def __call__(self, *filters: Any, **kwargs: Any) -> Callable[[Handler], Handler]:
        def decorator(callback: Handler) -> Handler:
            self.register(callback, *filters, **kwargs)
            return callback

        return decorator

    def filter(self, *filters: Any) -> None:
        self._root.filters = (*self._root.filters, *filters)

    def middleware(self, middleware: Any) -> Any:
        self.middlewares.append(middleware)
        return middleware

    outer_middleware = middleware

    async def check_root(self, event: Any, data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        return await self._root.check(event, data)

    async def trigger(self, event: Any, data: dict[str, Any]) -> Any:
        for handler in self.handlers:
            ok, extra = await handler.check(event, data)
            if not ok:
                continue
            try:
                return await handler.call(event, {**data, **extra, "handler": handler})
            except SkipHandler:
                continue
        return UNHANDLED


async def _noop(*_: Any, **__: Any) -> None:
    return None


class Router:
    def __init__(self, *, name: str | None = None) -> None:
        self.name = name or hex(id(self))
        self.message = EventObserver("message")
        self.callback_query = EventObserver("callback_query")
        self.observers: dict[str, EventObserver] = {
            "message": self.message,
            "callback_query": self.callback_query,
        }
        self.sub_routers: list[Router] = []
        self.parent_router: Router | None = None

    def include_router(self, router: Router) -> Router:
        if router.parent_router is not None:
            raise RuntimeError(f"Router {router.name!r} is already attached to {router.parent_router.name!r}")
        router.parent_router = self
        self.sub_routers.append(router)
        return router

    def include_routers(self, *routers: Router) -> None:
        for router in routers:
            self.include_router(router)

    async def propagate_event(self, update_type: str, event: Any, data: dict[str, Any]) -> Any:
        observer = self.observers.get(update_type)
        if observer is not None:
            ok, extra = await observer.check_root(event, data)
            if ok:
                response = await observer.trigger(event, {**data, **extra})
                if response is not UNHANDLED:
                    return response
        for router in self.sub_routers:
            response = await router.propagate_event(update_type, event, data)
            if response is not UNHANDLED:
                return response
        return UNHANDLED

    def __repr__(self) -> str:
        return f"<Router {self.name!r}>"


class Dispatcher(Router):
    """The root router. `feed_update` is the single entry point for every Discord event."""

    def __init__(self, *, storage: RedisStorage, name: str | None = None, **workflow_data: Any) -> None:
        super().__init__(name=name or "dispatcher")
        self.storage = storage
        self.update = EventObserver("update")
        self.workflow_data = workflow_data
        self._ids = itertools.count(1)

    def next_update_id(self) -> int:
        return next(self._ids)

    async def feed_update(self, bot: Any, update: Update, **kwargs: Any) -> Any:
        event = update.event
        from_user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None and getattr(event, "message", None) is not None:
            chat = event.message.chat

        data: dict[str, Any] = {
            **self.workflow_data,
            **kwargs,
            "bot": bot,
            "dispatcher": self,
            "event_update": update,
            "event_from_user": from_user,
            "event_chat": chat,
            "fsm_storage": self.storage,
        }
        if from_user is not None and chat is not None:
            state = FSMContext(self.storage, StorageKey(chat_id=chat.id, user_id=from_user.id))
            data["state"] = state
            data["raw_state"] = await state.get_state()

        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]] = self._listen_update
        for middleware in reversed(self.update.middlewares):
            handler = partial(middleware, handler)
        return await handler(update, data)

    async def _listen_update(self, update: Update, data: dict[str, Any]) -> Any:
        if update.event is None:
            return UNHANDLED
        return await self.propagate_event(update.event_type, update.event, data)


class BaseMiddleware:
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:  # pragma: no cover - interface
        return await handler(event, data)


__all__ = [
    "UNHANDLED",
    "BaseMiddleware",
    "CancelHandler",
    "Dispatcher",
    "EventObserver",
    "Router",
    "SkipHandler",
]
