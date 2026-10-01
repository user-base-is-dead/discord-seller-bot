from __future__ import annotations

import json

import pytest

from app.bot.framework import F, Router
from app.bot.framework.filters import Command, CommandStart, StateFilter
from app.bot.framework.filters.callback_data import CallbackData
from app.bot.framework.fsm.state import State, StatesGroup
from app.bot.framework.fsm.storage import FSMContext, RedisStorage, StorageKey
from app.bot.framework.markdown import html_to_markdown
from app.bot.framework.render import MAX_COMPONENTS, build_layout, component_count, copy_value, strip_dedupe
from app.bot.framework.router import UNHANDLED, Dispatcher, SkipHandler
from app.bot.framework.types import (
    CallbackQuery,
    Chat,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from app.core.redis import MemoryRedis


class ItemCB(CallbackData, prefix="it"):
    action: str
    id: str = ""
    page: int = 1


class Form(StatesGroup):
    name = State()
    price = State()


def _msg(text: str | None, chat_type: str = "private") -> Message:
    return Message(message_id=1, chat=Chat(id=5, type=chat_type), from_user=User(id=5), content=text)


# ---- callback data / magic filter ------------------------------------------------------------


def test_callback_data_round_trip_and_defaults():
    packed = ItemCB(action="view", id="42").pack()
    assert packed == "it:view:42:1"
    assert ItemCB.unpack("it:view::") == ItemCB(action="view", id="", page=1)
    with pytest.raises(ValueError):
        ItemCB(action="x" * 120).pack()
    with pytest.raises(ValueError):
        ItemCB(action="a:b").pack()


async def test_callback_filter_with_magic_rule():
    flt = ItemCB.filter(F.action.in_(["view", "edit"]) & (F.page == 2))
    query = CallbackQuery(id="1", from_user=User(id=1), data="it:edit:7:2", message=None)
    result = await flt(query)
    assert result == {"callback_data": ItemCB(action="edit", id="7", page=2)}
    assert await flt(CallbackQuery(id="1", from_user=User(id=1), data="it:edit:7:1", message=None)) is False
    assert await flt(CallbackQuery(id="1", from_user=User(id=1), data="other:x", message=None)) is False


def test_magic_filter_missing_attributes_are_falsy():
    assert F.data.startswith("x").resolve(CallbackQuery(id="1", from_user=User(id=1), data="xyz", message=None))
    assert not F.document.resolve(_msg("hi"))
    assert (~F.document).resolve(_msg("hi"))
    assert F.chat.type.in_({"group", "supergroup"}).resolve(_msg("hi", "supergroup"))


# ---- command / state filters -----------------------------------------------------------------


async def test_command_filter_parses_args():
    result = await Command("done")(_msg("/done 12 extra words"))
    assert result["command"].args == "12 extra words"
    assert await Command("done")(_msg("/doneX")) is False
    assert await Command("done")(_msg("done")) is False
    assert await CommandStart()(_msg("/start ref_abc"))


async def test_state_filter_and_state_names():
    assert Form.name.state == "Form:name"
    assert "Form:price" in Form
    assert await StateFilter(Form)(_msg("x"), raw_state="Form:price")
    assert not await StateFilter(Form.name)(_msg("x"), raw_state="Form:price")
    assert await StateFilter(None)(_msg("x"), raw_state=None)


async def test_fsm_storage_round_trips_enums():
    from app.database.models.catalog import FulfillmentMode

    ctx = FSMContext(RedisStorage(MemoryRedis()), StorageKey(chat_id=1, user_id=2))
    await ctx.set_state(Form.price)
    await ctx.update_data(mode=FulfillmentMode.MANUAL, nested={"m": [FulfillmentMode.AUTO]}, n=3)
    assert await ctx.get_state() == "Form:price"
    data = await ctx.get_data()
    assert data["mode"] is FulfillmentMode.MANUAL and data["nested"]["m"][0] is FulfillmentMode.AUTO
    await ctx.clear()
    assert await ctx.get_state() is None and await ctx.get_data() == {}


# ---- router propagation ----------------------------------------------------------------------


async def test_router_order_filters_and_skip_handler():
    calls: list[str] = []
    dp = Dispatcher(storage=RedisStorage(MemoryRedis()))
    gated, open_router = Router(name="gated"), Router(name="open")

    async def never(event, **_):
        return False

    gated.message.filter(never)

    @gated.message()
    async def unreachable(message):
        calls.append("gated")

    @open_router.message(Command("x"))
    async def skipper(message, command):
        calls.append(f"skip:{command.args}")
        raise SkipHandler

    @open_router.message()
    async def catch_all(message, state, raw_state):
        calls.append(f"catch:{raw_state}")
        return "ok"

    dp.include_routers(gated, open_router)
    result = await dp.feed_update(None, Update(update_id=1, message=_msg("/x 1")))
    assert result == "ok" and calls == ["skip:1", "catch:None"]

    empty = Dispatcher(storage=RedisStorage(MemoryRedis()))
    assert await empty.feed_update(None, Update(update_id=2, message=_msg("hi"))) is UNHANDLED


# ---- markdown --------------------------------------------------------------------------------


def test_markdown_conversion():
    assert html_to_markdown("<b>Hi</b> <i>there</i>") == "**Hi** *there*"
    assert html_to_markdown("a*b_c [x](y)") == "a\\*b\\_c \\[x\\](y)"
    assert html_to_markdown("<code>a_b*c</code>") == "`a_b*c`"
    assert html_to_markdown("<blockquote>one\ntwo</blockquote>after") == "> one\n> two\nafter"
    assert html_to_markdown("x <md>**raw** `c`</md>") == "x **raw** `c`"
    assert html_to_markdown("<mention>123</mention>") == "<@123>"
    assert html_to_markdown("/status <id> &lt;b&gt;") == "/status \\<id\\> \\<b\\>"
    assert html_to_markdown('<a href="https://ex.com/a_b">Open</a>') == "[Open](<https://ex.com/a_b>)"
    assert html_to_markdown("see https://ex.com/a_b_c") == "see https://ex.com/a_b_c"
    assert html_to_markdown("- dash\n# hash\n1. one") == "\\- dash\n\\# hash\n1\\. one"
    assert html_to_markdown("<pre>x = 1</pre>next") == "```\nx = 1\n```\nnext"
    assert html_to_markdown("text\n" + "\u2800" * 30) == "text"


# ---- renderer --------------------------------------------------------------------------------


def _markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def test_render_small_keyboard_keeps_buttons_and_styles():
    layout = build_layout(
        "hello",
        _markup(
            [
                [InlineKeyboardButton(text="Buy", callback_data="prod:buy:1", style="success")],
                [
                    InlineKeyboardButton(text="1/3", callback_data="noop"),
                    InlineKeyboardButton(text="Back", callback_data="nav:home", style="danger"),
                    InlineKeyboardButton(text="Home", callback_data="nav:home", style="primary"),
                ],
                [InlineKeyboardButton(text="Copy", copy_text=CopyTextButton(text="0xabc"))],
                [InlineKeyboardButton(text="Site", url="https://example.com")],
            ]
        ),
    )
    payload = json.dumps(layout.to_components())
    assert '"custom_id": "prod:buy:1"' in payload and '"style": 3' in payload
    assert '"disabled": true' in payload  # noop
    assert '"custom_id": "nav:home#1"' in payload  # duplicate id de-duplicated
    assert strip_dedupe("nav:home#1") == "nav:home"
    assert '"custom_id": "copy:0xabc"' in payload and copy_value("copy:0xabc") == "0xabc"
    assert '"url": "https://example.com"' in payload and '"style": 5' in payload
    assert layout.is_finished()  # never stored by discord.py: routed by custom_id instead


def test_render_long_list_becomes_select_within_budget():
    rows = [[InlineKeyboardButton(text=f"Item {i}", callback_data=f"prod:view:{i}")] for i in range(60)]
    rows.append([InlineKeyboardButton(text="Back", callback_data="nav:home")])
    layout = build_layout("list", _markup(rows))
    assert component_count(layout) <= MAX_COMPONENTS
    payload = layout.to_components()[0]["components"]
    selects = [c["components"][0] for c in payload if c["type"] == 1 and c["components"][0]["type"] == 3]
    assert len(selects) == 3 and sum(len(s["options"]) for s in selects) == 60
    assert selects[0]["options"][5]["value"] == "prod:view:5"
    assert payload[-1]["components"][0]["custom_id"] == "nav:home"  # the way out survives


def test_render_truncates_labels():
    layout = build_layout("x", _markup([[InlineKeyboardButton(text="y" * 200, callback_data="a")]]))
    row = next(c for c in layout.to_components()[0]["components"] if c["type"] == 1)  # the action row
    button = row["components"][0]
    assert len(button["label"]) == 80
