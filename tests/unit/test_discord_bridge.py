"""The Discord-facing half of the framework, against fake discord.py objects.

The integration tests replace the transport; these cover it: how an interaction is answered, how
gateway events become Messages/CallbackQueries, how the Bot facade addresses DMs and threads, and
that every slash command serialises into a payload Discord accepts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from app.bot.framework.bot import Bot
from app.bot.framework.client import OPEN_STORE, StoreClient
from app.bot.framework.exceptions import BotBadRequest, BotForbiddenError, translate
from app.bot.framework.interaction import InteractionReplier
from app.bot.framework.render import build_layout
from app.bot.framework.router import Dispatcher
from app.bot.framework.types import CallbackQuery, Message
from app.core.redis import MemoryRedis

# ---- fakes ----------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def edit_message(self, **kw):
        self._done = True
        self.calls.append(("edit_message", kw))

    async def send_message(self, content=None, **kw):
        self._done = True
        self.calls.append(("send_message", {"content": content, **kw}))
        return SimpleNamespace(message_id=555)

    async def defer(self, **kw):
        self._done = True
        self.calls.append(("defer", kw))


class FakeFollowup:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def send(self, content=None, **kw):
        self.calls.append(("followup", {"content": content, **kw}))
        return SimpleNamespace(delete=_async_noop)


async def _async_noop(*_, **__):
    return None


class FakeInteraction:
    def __init__(self, *, message=None, channel=None, guild_id=None, user_id=7, data=None, type_=None) -> None:
        self.id = 1
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = message
        self.channel = channel
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id, bot=False, name="alice", global_name="Alice", display_name="Alice")
        self.data = data or {}
        self.type = type_ or discord.InteractionType.component
        self.calls: list[tuple[str, dict]] = []

    async def edit_original_response(self, **kw):
        self.calls.append(("edit_original", kw))
        return SimpleNamespace(id=556)

    async def delete_original_response(self):
        self.calls.append(("delete_original", {}))

    async def original_response(self):
        return SimpleNamespace(id=557)


def _layout(text="screen"):
    return build_layout(text)


# ---- interaction replier --------------------------------------------------------------------


async def test_component_edit_then_notify_uses_followup():
    interaction = FakeInteraction(message=SimpleNamespace(edit=_async_noop))
    replier = InteractionReplier(interaction, kind="component")
    await replier.edit_component_message(_layout())
    await replier.notify("Saved!", alert=False)
    assert interaction.response.calls[0][0] == "edit_message"
    assert interaction.followup.calls[0][1]["ephemeral"] is True
    assert interaction.followup.calls[0][1]["content"] == "Saved!"


async def test_component_notify_first_then_edit_goes_to_message():
    edits: list[dict] = []

    async def edit(**kw):
        edits.append(kw)

    interaction = FakeInteraction(message=SimpleNamespace(edit=edit))
    replier = InteractionReplier(interaction, kind="component")
    await replier.notify("Out of stock", alert=True)
    await replier.edit_component_message(_layout())
    assert interaction.response.calls[0] == ("send_message", {"content": "Out of stock", "ephemeral": True, "delete_after": None})
    assert edits and "view" in edits[0]  # edited the component's message, not the ephemeral note


async def test_ack_then_edit_uses_original_response():
    interaction = FakeInteraction(message=SimpleNamespace(edit=_async_noop))
    replier = InteractionReplier(interaction, kind="component")
    await replier.ack()
    await replier.edit_component_message(_layout())
    assert interaction.response.calls[0][0] == "defer"
    assert interaction.calls[0][0] == "edit_original"


async def test_command_first_answer_is_the_response_then_finalize():
    interaction = FakeInteraction()
    replier = InteractionReplier(interaction, kind="command")
    first = await replier.send_first(_layout())
    second = await replier.send_first(_layout())
    assert first == 555 and second is None
    await replier.finalize()
    assert [c[0] for c in interaction.response.calls] == ["send_message"]


async def test_command_deferred_then_answered_edits_original_and_unanswered_is_cleaned():
    interaction = FakeInteraction()
    replier = InteractionReplier(interaction, kind="command")
    await replier.ack()
    await replier.send_first(_layout())
    assert interaction.calls[0][0] == "edit_original"

    silent = FakeInteraction()
    replier2 = InteractionReplier(silent, kind="command")
    await replier2.ack()
    await replier2.finalize()
    assert silent.calls == [("delete_original", {})]


async def test_unanswered_component_is_deferred_on_finalize():
    interaction = FakeInteraction()
    replier = InteractionReplier(interaction, kind="component")
    await replier.finalize()
    assert interaction.response.calls == [("defer", {})]


def test_translate_maps_discord_errors():
    response = SimpleNamespace(status=403, reason="Forbidden")
    assert isinstance(translate(discord.Forbidden(response, {"code": 50007, "message": "Cannot DM"})), BotForbiddenError)
    unknown = discord.NotFound(SimpleNamespace(status=404, reason="nf"), {"code": 10062, "message": "Unknown interaction"})
    assert "query is too old" in str(translate(unknown))
    bad = discord.HTTPException(SimpleNamespace(status=400, reason="bad"), {"code": 50035, "message": "Invalid Form Body"})
    assert isinstance(translate(bad), BotBadRequest)


# ---- client bridge --------------------------------------------------------------------------


def _client(staff=(900,), commands=()) -> tuple[StoreClient, list[Any]]:
    dp = Dispatcher(storage=__import__("app.bot.framework.fsm.storage", fromlist=["RedisStorage"]).RedisStorage(MemoryRedis()))
    seen: list[Any] = []

    async def capture(bot, update, **_):
        seen.append(update)

    dp.feed_update = capture  # type: ignore[assignment]
    client = StoreClient(dispatcher=dp, staff_channel_ids=staff, commands=commands, sync_commands=False)
    return client, seen


class _DM:
    def __init__(self, channel_id=42) -> None:
        self.id = channel_id


def _patch_dm(monkeypatch):
    monkeypatch.setattr(discord, "DMChannel", _DM)


def _author(user_id=7, bot=False):
    return SimpleNamespace(id=user_id, bot=bot, name="alice", global_name="Alice", display_name="Alice")


async def test_dm_message_becomes_private_message(monkeypatch):
    _patch_dm(monkeypatch)
    client, seen = _client()
    message = SimpleNamespace(
        author=_author(),
        type=discord.MessageType.default,
        channel=_DM(42),
        guild=None,
        id=1001,
        content="hello **there**",
        attachments=[],
        stickers=[],
        created_at=None,
        webhook_id=None,
    )
    await client.on_message(message)
    shim = seen[0].message
    assert shim.chat.type == "private" and shim.chat.id == 7 and shim.text == "hello **there**"
    assert shim.html_text == "<md>hello **there**</md>"
    assert client.bot._dm_channels[7] == 42


async def test_bot_and_public_channel_messages_are_ignored(monkeypatch):
    _patch_dm(monkeypatch)
    client, seen = _client()
    base = dict(type=discord.MessageType.default, id=1, content="x", attachments=[], stickers=[], created_at=None, webhook_id=None)
    await client.on_message(SimpleNamespace(author=_author(bot=True), channel=_DM(), guild=None, **base))
    public = SimpleNamespace(id=12345)
    await client.on_message(SimpleNamespace(author=_author(), channel=public, guild=SimpleNamespace(id=1), **base))
    assert seen == []


async def test_staff_thread_message_carries_parent_and_thread(monkeypatch):
    class FakeThread:
        def __init__(self) -> None:
            self.id = 5000
            self.parent_id = 900
            self.parent = SimpleNamespace(name="support")

    monkeypatch.setattr(discord, "Thread", FakeThread)
    client, seen = _client(staff=(900,))
    message = SimpleNamespace(
        author=_author(),
        type=discord.MessageType.default,
        channel=FakeThread(),
        guild=SimpleNamespace(id=1),
        id=1,
        content="reply",
        attachments=[],
        stickers=[],
        created_at=None,
        webhook_id=None,
    )
    await client.on_message(message)
    shim = seen[0].message
    assert shim.chat.id == 900 and shim.chat.type == "supergroup" and shim.message_thread_id == 5000


async def test_uncached_thread_message_is_resolved_and_routed(monkeypatch):
    class FakeThread:
        def __init__(self) -> None:
            self.id = 5000
            self.parent_id = 900
            self.parent = SimpleNamespace(name="support")

    monkeypatch.setattr(discord, "Thread", FakeThread)
    client, seen = _client(staff=(900,))
    fetched: list[int] = []

    async def fetch_channel(channel_id):
        fetched.append(channel_id)
        return FakeThread()

    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    partial = discord.PartialMessageable(state=client._connection, id=5000, guild_id=1)
    base = dict(type=discord.MessageType.default, content="reply", attachments=[], stickers=[], created_at=None, webhook_id=None)
    for message_id in (1, 2):
        await client.on_message(
            SimpleNamespace(author=_author(), channel=partial, guild=SimpleNamespace(id=1), id=message_id, **base)
        )
    assert [u.message.message_thread_id for u in seen] == [5000, 5000]
    assert fetched == [5000]  # fetched once, then remembered


async def test_button_and_select_become_callback_queries(monkeypatch):
    _patch_dm(monkeypatch)
    client, seen = _client()
    source = SimpleNamespace(id=77, flags=SimpleNamespace(components_v2=True), attachments=[], author=_author(1, bot=True))
    await client.on_interaction(FakeInteraction(message=source, channel=_DM(), data={"custom_id": "nav:home#1"}))
    await client.on_interaction(FakeInteraction(message=source, channel=_DM(), data={"custom_id": "sel:0", "values": ["prod:view:5#2"]}))
    queries = [u.callback_query for u in seen]
    assert [q.data for q in queries] == ["nav:home", "prod:view:5"]
    assert isinstance(queries[0], CallbackQuery) and isinstance(queries[0].message, Message)
    assert queries[0].message._editable


async def test_copy_button_answers_with_value_only():
    client, seen = _client()
    interaction = FakeInteraction(data={"custom_id": "copy:5.2025"})
    await client.on_interaction(interaction)
    assert interaction.response.calls == [("send_message", {"content": "5.2025", "ephemeral": True})]
    assert seen == []


async def test_guild_button_outside_staff_channel_is_refused():
    client, seen = _client()
    interaction = FakeInteraction(channel=SimpleNamespace(id=333), guild_id=1, data={"custom_id": "nav:home"})
    await client.on_interaction(interaction)
    assert "only works in your DMs" in interaction.response.calls[0][1]["content"]
    assert seen == []


async def test_open_store_button_starts_store_in_dms():
    client, seen = _client()
    interaction = FakeInteraction(channel=SimpleNamespace(id=333), guild_id=1, data={"custom_id": OPEN_STORE})
    await client.on_interaction(interaction)
    assert "DMs" in interaction.response.calls[0][1]["content"]
    message = seen[0].message
    assert message.text == "/start" and message.chat.type == "private" and message.chat.id == 7


async def test_slash_command_in_public_channel_is_answered_in_dms():
    client, seen = _client()
    interaction = FakeInteraction(channel=SimpleNamespace(id=333), guild_id=1, type_=discord.InteractionType.application_command)
    await client._on_command(interaction, "wallet", None)
    assert interaction.response.calls[0][1]["ephemeral"] is True
    message = seen[0].message
    assert message.text == "/wallet" and message.chat.type == "private"
    assert message._replier.answers_in_place is False


async def test_slash_command_args_become_text(monkeypatch):
    _patch_dm(monkeypatch)
    client, seen = _client()
    interaction = FakeInteraction(channel=_DM(), type_=discord.InteractionType.application_command)
    await client._on_command(interaction, "done", "  12  ")
    assert seen[0].message.text == "/done 12"


def test_every_slash_command_serialises():
    from app.bot.commands import ALL_COMMANDS

    client, _ = _client(commands=ALL_COMMANDS)
    payloads = [command.to_dict(client.tree) for command in client.tree.get_commands()]
    names = [p["name"] for p in payloads]
    assert len(names) == len(set(names)) == len(ALL_COMMANDS)
    for payload in payloads:
        assert 1 <= len(payload["description"]) <= 100
        assert payload["name"].islower() and len(payload["name"]) <= 32
    admin = next(p for p in payloads if p["name"] == "admin")
    assert int(admin["default_member_permissions"]) == discord.Permissions(manage_guild=True).value
    start = next(p for p in payloads if p["name"] == "start")
    assert start.get("default_member_permissions") is None  # user commands are visible to everyone
    done = next(p for p in payloads if p["name"] == "done")
    assert done["options"][0]["name"] == "args" and done["options"][0]["required"] is False


# ---- Bot facade -----------------------------------------------------------------------------


class _Sendable:
    def __init__(self, cid: int) -> None:
        self.id = cid
        self.sent: list[dict] = []

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=999, channel=self, author=_author(1, bot=True))


class FakeDiscordClient:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=1, bot=True, name="bot", global_name=None, display_name="bot")
        self.channels: dict[int, Any] = {}
        self.users: dict[int, Any] = {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), {"code": 10013, "message": "Unknown User"})

    async def fetch_channel(self, cid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), {"code": 10003, "message": "Unknown Channel"})

    def get_partial_messageable(self, cid, **_):
        return self.channels.setdefault(cid, _Sendable(cid))


async def test_bot_sends_dm_to_user_id_and_caches_channel():
    fake = FakeDiscordClient()
    dm = _Sendable(4242)

    async def create_dm():
        return dm

    fake.users[7] = SimpleNamespace(dm_channel=None, create_dm=create_dm)
    bot = Bot(fake, accent_colour=0x5865F2)
    sent = await bot.send_message(7, "<b>Hello</b>", reply_markup=None)
    assert sent.message_id == 999 and "view" in dm.sent[0]
    assert dm.sent[0]["view"].to_components()[0]["components"][0]["content"] == "**Hello**"
    assert bot._dm_channels[7] == 4242


async def test_bot_thread_send_uses_thread_id_and_unknown_target_raises():
    fake = FakeDiscordClient()
    thread = _Sendable(5000)
    fake.channels[5000] = thread
    bot = Bot(fake, known_channels={900})
    await bot.send_message(900, "in thread", message_thread_id=5000)
    assert thread.sent
    with pytest.raises(BotBadRequest):
        await bot.send_message(900, "nowhere", message_thread_id=6000)


async def test_bot_topic_lifecycle_on_threads(monkeypatch):
    edits: list[dict] = []

    class FakeThread:
        def __init__(self) -> None:
            self.id = 5000

        async def edit(self, **kw):
            edits.append(kw)

    monkeypatch.setattr(discord, "Thread", FakeThread)
    fake = FakeDiscordClient()
    fake.channels[5000] = FakeThread()
    bot = Bot(fake, known_channels={900})
    await bot.close_forum_topic(900, 5000)
    await bot.reopen_forum_topic(900, 5000)
    assert edits == [{"archived": True, "locked": True}, {"archived": False, "locked": False}]


def test_auto_ack_is_cancellable():
    from app.bot.framework.interaction import auto_ack

    async def run():
        replier = InteractionReplier(FakeInteraction(), kind="component")
        task = asyncio.create_task(auto_ack(replier, delay=10))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return replier.interaction.response.calls

    assert asyncio.run(run()) == []
