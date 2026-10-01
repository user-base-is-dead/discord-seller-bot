"""FSM states, named exactly as aiogram names them: `"<GroupName>:<state>"`.

The ban check and the pending-payment rule both compare raw state strings against
`TicketForm.__name__`, so the naming scheme is part of the contract, not a detail.
"""

from __future__ import annotations

from typing import Any


class State:
    def __init__(self, state: str | None = None, group_name: str | None = None) -> None:
        self._state = state
        self._group_name = group_name
        self._group: type[StatesGroup] | None = None

    @property
    def group(self) -> type[StatesGroup] | None:
        return self._group

    @property
    def state(self) -> str | None:
        if self._state is None or self._state == "*":
            return self._state
        if self._group_name is not None:
            group = self._group_name
        elif self._group is not None:
            group = self._group.__full_group_name__
        else:
            group = "@"
        return f"{group}:{self._state}"

    def set_parent(self, group: type[StatesGroup]) -> None:
        self._group = group

    def __set_name__(self, owner: type, name: str) -> None:
        if self._state is None:
            self._state = name
        if isinstance(owner, StatesGroupMeta):
            self.set_parent(owner)

    def __call__(self, event: Any = None, raw_state: str | None = None) -> bool:
        if self.state == "*":
            return True
        return raw_state == self.state

    def __eq__(self, other: object) -> bool:
        if isinstance(other, State):
            return self.state == other.state
        if isinstance(other, str):
            return self.state == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.state)

    def __repr__(self) -> str:
        return f"<State {self.state!r}>"

    __str__ = __repr__


class StatesGroupMeta(type):
    __states__: tuple[State, ...]
    __state_names__: tuple[str, ...]
    __full_group_name__: str

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        states = [value for value in namespace.values() if isinstance(value, State)]
        cls.__full_group_name__ = name
        for state in states:
            state.set_parent(cls)  # type: ignore[arg-type]
        cls.__states__ = tuple(states)
        cls.__state_names__ = tuple(s.state for s in states if s.state)
        return cls

    def __contains__(cls, item: object) -> bool:
        if isinstance(item, str):
            return item in cls.__state_names__
        if isinstance(item, State):
            return item in cls.__states__
        return False

    def __call__(cls, event: Any = None, raw_state: str | None = None) -> bool:  # type: ignore[override]
        return raw_state in cls.__state_names__

    def __str__(cls) -> str:
        return f"<StatesGroup {cls.__full_group_name__!r}>"


class StatesGroup(metaclass=StatesGroupMeta):
    pass


default_state = State()
any_state = State(state="*")
