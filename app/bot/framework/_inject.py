"""aiogram-style dependency injection: a handler or filter receives only the keyword arguments its
signature names (`session`, `user`, `state`, `callback_data`, …), or everything when it takes
`**kwargs`. Signatures are inspected once per callable."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import lru_cache
from typing import Any

_KEYWORD_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _key(func: Callable[..., Any]) -> tuple[Callable[..., Any], int]:
    """What to inspect, and how many leading positional parameters are not injectable.

    A function or bound method takes the event first. A callable instance (a Filter) is inspected
    through its class's `__call__`, whose first two parameters are `self` and the event — keyed on
    the class so filters built per call never grow the cache.
    """
    if inspect.isfunction(func) or inspect.ismethod(func) or inspect.isbuiltin(func):
        return func, 1
    return type(func).__call__, 2


@lru_cache(maxsize=4096)
def _spec(func: Callable[..., Any], skip: int) -> tuple[bool, frozenset[str]]:
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return True, frozenset()
    varkw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    names = frozenset(p.name for p in params[skip:] if p.kind in _KEYWORD_KINDS)
    return varkw, names


def injectable_kwargs(func: Callable[..., Any], data: dict[str, Any]) -> dict[str, Any]:
    varkw, names = _spec(*_key(func))
    if varkw:
        return dict(data)
    return {k: v for k, v in data.items() if k in names}


async def call_with_injection(func: Callable[..., Any], event: Any, data: dict[str, Any]) -> Any:
    result = func(event, **injectable_kwargs(func, data))
    if inspect.isawaitable(result):
        result = await result
    return result
