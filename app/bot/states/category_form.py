from __future__ import annotations

from app.bot.framework.fsm.state import State, StatesGroup


class CategoryForm(StatesGroup):
    name = State()
    emoji = State()
    description = State()
    confirm = State()
