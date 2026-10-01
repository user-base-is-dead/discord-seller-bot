"""`F` — a small magic filter covering what the handlers write.

    F.action == "list"            F.data.startswith("pedit:")
    F.action.in_(["ban", "unban"])  F.chat.type.in_({"group", "supergroup"})
    (F.action == "create") & (F.category != "")        ~F.document

A MagicFilter is a deferred expression. Building it (attribute access, calls, comparisons) never
touches an event; `resolve(obj)` evaluates it against one. Any attribute that is missing or None
along the way makes the whole expression falsy instead of raising, which is how aiogram behaves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_MISSING = object()


class MagicFilter:
    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[Any], Any] | None = None) -> None:
        object.__setattr__(self, "_fn", fn or (lambda obj: obj))

    # -- evaluation ---------------------------------------------------------------------------

    def resolve(self, obj: Any) -> Any:
        try:
            value = self._fn(obj)
        except Exception:
            return False
        return False if value is _MISSING else value

    # -- building -----------------------------------------------------------------------------

    def _then(self, step: Callable[[Any], Any]) -> MagicFilter:
        prev = self._fn

        def fn(obj: Any) -> Any:
            value = prev(obj)
            if value is _MISSING:
                return _MISSING
            return step(value)

        return MagicFilter(fn)

    def __getattr__(self, name: str) -> MagicFilter:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)

        def step(value: Any) -> Any:
            if value is None:
                return _MISSING
            return getattr(value, name, _MISSING)

        return self._then(step)

    def __call__(self, *args: Any, **kwargs: Any) -> MagicFilter:
        return self._then(lambda value: value(*args, **kwargs))

    def __eq__(self, other: object) -> MagicFilter:  # type: ignore[override]
        return self._then(lambda value: value == other)

    def __ne__(self, other: object) -> MagicFilter:  # type: ignore[override]
        return self._then(lambda value: value != other)

    def in_(self, container: Any) -> MagicFilter:
        return self._then(lambda value: value in container)

    def len(self) -> MagicFilter:
        return self._then(len)

    def __and__(self, other: MagicFilter) -> MagicFilter:
        return MagicFilter(lambda obj: bool(self.resolve(obj)) and bool(_resolve(other, obj)))

    def __or__(self, other: MagicFilter) -> MagicFilter:
        return MagicFilter(lambda obj: bool(self.resolve(obj)) or bool(_resolve(other, obj)))

    def __invert__(self) -> MagicFilter:
        return MagicFilter(lambda obj: not self.resolve(obj))

    __hash__ = object.__hash__


def _resolve(value: Any, obj: Any) -> Any:
    return value.resolve(obj) if isinstance(value, MagicFilter) else value


F = MagicFilter()
