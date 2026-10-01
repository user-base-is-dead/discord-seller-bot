"""Filters with aiogram's semantics.

A filter returns False to reject, True to accept, or a dict to accept *and* inject values into the
handler (`Command` injects `command`, `CallbackData.filter()` injects `callback_data`).

Commands arrive as text either way: a Discord slash command is turned into a message whose text is
`/<name> <args>` before dispatch, and a command typed as plain text in a DM is already one. So one
`Command("done")` matches `/done 12` whether it came from the slash picker or the keyboard.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.bot.framework._inject import call_with_injection
from app.bot.framework.fsm.state import State, StatesGroupMeta
from app.bot.framework.magic import MagicFilter


class Filter(ABC):
    @abstractmethod
    async def __call__(self, *args: Any, **kwargs: Any) -> bool | dict[str, Any]: ...

    def __invert__(self) -> _InvertFilter:
        return _InvertFilter(self)


class _InvertFilter(Filter):
    def __init__(self, target: Any) -> None:
        self.target = target

    async def __call__(self, event: Any, **data: Any) -> bool:
        return not bool(await evaluate_filter(self.target, event, data))


async def evaluate_filter(flt: Any, event: Any, data: dict[str, Any]) -> bool | dict[str, Any]:
    """Run any supported filter shape against an event."""
    if isinstance(flt, MagicFilter):
        return bool(flt.resolve(event))
    if isinstance(flt, State) or isinstance(flt, StatesGroupMeta):
        return bool(flt(event, raw_state=data.get("raw_state")))
    if isinstance(flt, bool):
        return flt
    result = await call_with_injection(flt, event, data)
    if isinstance(result, dict):
        return result
    return bool(result)


@dataclass(frozen=True)
class CommandObject:
    prefix: str = "/"
    command: str = ""
    mention: str | None = None
    args: str | None = field(default=None)

    @property
    def text(self) -> str:
        line = f"{self.prefix}{self.command}"
        if self.mention:
            line += f"@{self.mention}"
        if self.args:
            line += f" {self.args}"
        return line


def parse_command(text: str | None, prefixes: str = "/") -> CommandObject | None:
    if not text:
        return None
    text = text.strip()
    if not text or text[0] not in prefixes:
        return None
    head, *rest = text.split(maxsplit=1)
    name, _, mention = head[1:].partition("@")
    if not name:
        return None
    return CommandObject(prefix=head[0], command=name, mention=mention or None, args=rest[0] if rest else None)


class Command(Filter):
    def __init__(self, *commands: str, prefix: str = "/", ignore_case: bool = False) -> None:
        if not commands:
            raise ValueError("Command filter needs at least one command")
        self.ignore_case = ignore_case
        self.commands = tuple(c.lower() if ignore_case else c for c in (c.lstrip("/") for c in commands))
        self.prefix = prefix

    async def __call__(self, message: Any, **_: Any) -> bool | dict[str, Any]:
        text = getattr(message, "text", None) or getattr(message, "caption", None)
        command = parse_command(text, self.prefix)
        if command is None:
            return False
        name = command.command.lower() if self.ignore_case else command.command
        if name not in self.commands:
            return False
        return {"command": command}


class CommandStart(Command):
    def __init__(self, deep_link: bool = False) -> None:
        super().__init__("start")
        self.deep_link = deep_link

    async def __call__(self, message: Any, **data: Any) -> bool | dict[str, Any]:
        result = await super().__call__(message, **data)
        if not result:
            return False
        if self.deep_link and not result["command"].args:
            return False
        return result


class StateFilter(Filter):
    """Matches when the current FSM state is any of the given states/groups. `None` means "no state"
    and `"*"` means "any state"."""

    def __init__(self, *states: Any) -> None:
        if not states:
            raise ValueError("StateFilter needs at least one state")
        self.states = states

    async def __call__(self, obj: Any, raw_state: str | None = None, **_: Any) -> bool:
        for allowed in self.states:
            if allowed == "*":
                return True
            if allowed is None:
                if raw_state is None:
                    return True
                continue
            if isinstance(allowed, str):
                if raw_state == allowed:
                    return True
                continue
            if isinstance(allowed, (State, StatesGroupMeta)):
                if allowed(obj, raw_state=raw_state):
                    return True
        return False


__all__ = [
    "Command",
    "CommandObject",
    "CommandStart",
    "Filter",
    "StateFilter",
    "evaluate_filter",
    "parse_command",
]
