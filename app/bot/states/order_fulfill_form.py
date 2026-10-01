from __future__ import annotations

from app.bot.framework.fsm.state import State, StatesGroup


class OrderFulfillForm(StatesGroup):
    payload = State()
