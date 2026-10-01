"""Drive the real bot — dispatcher, middlewares, handlers, services, SQLite — with Discord faked out.

`FakeBot` replaces only the transport: every screen still goes through the real renderer
(`Bot.render` → Components V2 layout, serialised with discord.py's own `to_components()`), so a
screen that Discord would reject — too many components, an over-long label, a duplicate custom id —
fails the test instead of failing in production.
"""

from __future__ import annotations

import io
import itertools
from dataclasses import dataclass, field
from typing import Any

import discord

from app.bot.framework.bot import Bot
from app.bot.framework.exceptions import BotBadRequest, BotForbiddenError, BotNotFound
from app.bot.framework.markdown import TEXT_LIMIT
from app.bot.framework.render import MAX_COMPONENTS, component_count
from app.bot.framework.router import Dispatcher
from app.bot.framework.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    Document,
    ForumTopic,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageId,
    PhotoSize,
    Update,
    User,
    _File,
)


@dataclass
class Screen:
    """What `render` produced: the inputs a test reads back, plus the real layout it validated."""

    text: str
    markup: Any
    layout: Any
    markdown: str


@dataclass
class Sent:
    message_id: int
    chat_id: int
    thread_id: int | None
    text: str
    markup: Any = None
    markdown: str = ""
    kind: str = "screen"  # screen | file | copy
    data: bytes | None = None
    filename: str | None = None
    deleted: bool = False
    edits: int = 0
    source: Any = None

    def buttons(self) -> list[InlineKeyboardButton]:
        if not isinstance(self.markup, InlineKeyboardMarkup):
            return []
        return [b for row in self.markup.inline_keyboard for b in row]

    def labels(self) -> list[str]:
        return [b.text for b in self.buttons()]


@dataclass
class Thread:
    thread_id: int
    chat_id: int
    name: str
    archived: bool = False
    locked: bool = False


@dataclass
class Notice:
    user_id: int
    text: str
    alert: bool


class FakeAttachment:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeBot(Bot):
    def __init__(self, staff_channels: set[int]) -> None:
        super().__init__(client=None, accent_colour=0x5865F2, known_channels=set(staff_channels))  # type: ignore[arg-type]
        self.staff_channels = set(staff_channels)
        self.sent: list[Sent] = []
        self.by_id: dict[int, Sent] = {}
        self.threads: dict[int, Thread] = {}
        self.blocked_users: set[int] = set()
        self.typed: dict[int, Message] = {}  # message_id -> a message a person sent
        self._ids = itertools.count(10_000)

    # -- identity / rendering ------------------------------------------------------------------

    @property
    def id(self) -> int:  # type: ignore[override]
        return 1

    def next_id(self) -> int:
        return next(self._ids)

    def render(self, text: str, reply_markup: Any = None, *, parse_mode: str | None = "HTML") -> Screen:  # type: ignore[override]
        layout = super().render(text, reply_markup, parse_mode=parse_mode)
        layout.to_components()  # serialise exactly as discord.py would before sending
        count = component_count(layout)
        assert count <= MAX_COMPONENTS, f"{count} components > {MAX_COMPONENTS}"
        assert layout.content_length() <= TEXT_LIMIT, "text over Discord's 4000-character limit"
        # Every text block of the card, in order (a card is cut into blocks at its dividers).
        markdown = "\n".join(
            item.content for item in layout.walk_children() if isinstance(item, discord.ui.TextDisplay)
        )
        return Screen(text=text, markup=reply_markup, layout=layout, markdown=markdown)

    def wrap_sent(self, sent: Any, chat: Chat, thread_id: int | None) -> Message:  # type: ignore[override]
        return Message(message_id=int(sent), chat=chat, bot=self, message_thread_id=thread_id)

    def _chat(self, chat_id: int) -> Chat:
        if chat_id in self.staff_channels:
            return Chat(id=chat_id, type="supergroup")
        return Chat(id=chat_id, type="private")

    def _check_target(self, chat_id: int, thread_id: int | None) -> None:
        if thread_id is not None:
            thread = self.threads.get(thread_id)
            if thread is None or thread.chat_id != chat_id:
                raise BotNotFound("Not Found: Unknown Channel")
            thread.archived = False  # posting into an archived thread un-archives it on Discord
            return
        if chat_id in self.blocked_users:
            raise BotForbiddenError("Forbidden: Cannot send messages to this user")

    def record(self, **fields: Any) -> Sent:
        sent = Sent(message_id=self.next_id(), **fields)
        self.sent.append(sent)
        self.by_id[sent.message_id] = sent
        return sent

    # -- screens ------------------------------------------------------------------------------

    async def send_message(  # type: ignore[override]
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any = None,
        message_thread_id: int | None = None,
        parse_mode: str | None = "HTML",
        reply_to_message_id: int | None = None,
        **_: Any,
    ) -> Message:
        self._check_target(chat_id, message_thread_id)
        screen = self.render(text, reply_markup, parse_mode=parse_mode)
        sent = self.record(
            chat_id=chat_id, thread_id=message_thread_id, text=text, markup=reply_markup, markdown=screen.markdown
        )
        return Message(message_id=sent.message_id, chat=self._chat(chat_id), bot=self, message_thread_id=message_thread_id)

    async def edit_message_text(  # type: ignore[override]
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        *,
        message_thread_id: int | None = None,
        reply_markup: Any = None,
        parse_mode: str | None = "HTML",
        **_: Any,
    ) -> Message:
        sent = self.by_id.get(int(message_id or 0))
        if sent is None or sent.deleted or sent.chat_id != chat_id:
            raise BotNotFound("Not Found: Unknown Message")
        if sent.kind != "screen":
            raise BotBadRequest("Bad Request: cannot convert a message to components v2")
        screen = self.render(text, reply_markup, parse_mode=parse_mode)
        sent.text, sent.markup, sent.markdown = text, reply_markup, screen.markdown
        sent.edits += 1
        return Message(message_id=sent.message_id, chat=self._chat(chat_id), bot=self, message_thread_id=message_thread_id)

    def apply_screen(self, sent: Sent, screen: Screen) -> None:
        if sent.kind != "screen":
            raise BotBadRequest("Bad Request: cannot convert a message to components v2")
        sent.text, sent.markup, sent.markdown = screen.text, screen.markup, screen.markdown
        sent.edits += 1

    async def delete_message(self, chat_id: int, message_id: int, *, message_thread_id: int | None = None) -> bool:  # type: ignore[override]
        sent = self.by_id.get(int(message_id))
        if sent is None:
            raise BotNotFound("Not Found: Unknown Message")
        sent.deleted = True
        return True

    # -- threads ------------------------------------------------------------------------------

    async def create_forum_topic(self, chat_id: int, name: str, **_: Any) -> ForumTopic:  # type: ignore[override]
        if chat_id not in self.staff_channels:
            raise BotBadRequest("Bad Request: the chat is not a forum or text channel")
        thread = Thread(thread_id=self.next_id(), chat_id=chat_id, name=name[:100])
        self.threads[thread.thread_id] = thread
        return ForumTopic(message_thread_id=thread.thread_id, name=thread.name)

    async def close_forum_topic(self, chat_id: int, message_thread_id: int, **_: Any) -> bool:  # type: ignore[override]
        thread = self.threads.get(message_thread_id)
        if thread is None:
            raise BotBadRequest("Bad Request: message thread not found")
        thread.archived = thread.locked = True
        return True

    async def reopen_forum_topic(self, chat_id: int, message_thread_id: int, **_: Any) -> bool:  # type: ignore[override]
        thread = self.threads.get(message_thread_id)
        if thread is None:
            raise BotBadRequest("Bad Request: message thread not found")
        thread.archived = thread.locked = False
        return True

    # -- files and copies ---------------------------------------------------------------------

    async def _send_file(  # type: ignore[override]
        self,
        chat_id: int,
        source: Any,
        *,
        caption: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: Any = None,
        parse_mode: str | None = "HTML",
    ) -> Message:
        self._check_target(chat_id, message_thread_id)
        if isinstance(source, BufferedInputFile):
            data, name = source.file, source.filename
        elif isinstance(source, _File):
            data, name = await source.attachment.read(), source.file_name
        else:
            data, name = None, str(source).rsplit("/", 1)[-1]
        sent = self.record(
            chat_id=chat_id,
            thread_id=message_thread_id,
            text=caption or "",
            markup=reply_markup,
            kind="file",
            data=data,
            filename=name,
        )
        return Message(message_id=sent.message_id, chat=self._chat(chat_id), bot=self, message_thread_id=message_thread_id)

    async def copy_message(  # type: ignore[override]
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        *,
        reply_markup: Any = None,
        message_thread_id: int | None = None,
        **_: Any,
    ) -> MessageId:
        source = self.typed.get(int(message_id))
        if source is None or source.chat.id != from_chat_id:
            raise BotNotFound("Not Found: Unknown Message")
        self._check_target(chat_id, message_thread_id)
        sent = self.record(
            chat_id=chat_id,
            thread_id=message_thread_id,
            text=source.text or source.caption or "",
            markup=reply_markup,
            kind="copy",
            source=source,
        )
        return MessageId(message_id=sent.message_id)

    # -- queries ------------------------------------------------------------------------------

    def in_chat(self, chat_id: int, thread_id: int | None = None, *, include_deleted: bool = False) -> list[Sent]:
        return [
            s
            for s in self.sent
            if s.chat_id == chat_id and s.thread_id == thread_id and (include_deleted or not s.deleted)
        ]


class FakeReplier:
    """The InteractionReplier contract, recorded instead of sent."""

    def __init__(self, harness: Harness, *, kind: str, user_id: int, target: Sent | None = None, chat: Chat | None = None,
                 thread_id: int | None = None, answers_in_place: bool = True) -> None:
        self.h = harness
        self.kind = kind
        self.user_id = user_id
        self.target = target
        self.chat = chat
        self.thread_id = thread_id
        self.answers_in_place = kind == "command" and answers_in_place
        self._first_used = False
        self.acked = False

    async def ack(self) -> None:
        self.acked = True

    async def edit_component_message(self, screen: Screen) -> None:
        assert self.target is not None, "edit without a component message"
        self.h.bot.apply_screen(self.target, screen)
        self.acked = True

    async def notify(self, text: str, *, alert: bool) -> None:
        self.h.notices.append(Notice(self.user_id, text, alert))
        self.acked = True

    async def send_first(self, screen: Screen) -> Any:
        if not self.answers_in_place or self._first_used:
            return None
        self._first_used = True
        self.acked = True
        sent = self.h.bot.record(
            chat_id=self.chat.id, thread_id=self.thread_id, text=screen.text, markup=screen.markup, markdown=screen.markdown
        )
        return sent.message_id

    async def finalize(self) -> None:
        self.acked = True


class Harness:
    def __init__(self, dp: Dispatcher, bot: FakeBot, sessionmaker: Any) -> None:
        self.dp = dp
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.notices: list[Notice] = []

    # -- people -------------------------------------------------------------------------------

    @staticmethod
    def person(user_id: int, username: str | None = None, name: str | None = None) -> User:
        return User(id=user_id, first_name=name or f"User{user_id}", username=username or f"user{user_id}")

    # -- sending ------------------------------------------------------------------------------

    async def settle(self, timeout: float = 10.0) -> None:
        """Wait for background work a handler spawned (broadcast workers) to finish."""
        import asyncio

        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    def _message(
        self,
        user: User,
        text: str | None,
        *,
        chat: Chat,
        thread_id: int | None = None,
        files: list[tuple[str, bytes]] | None = None,
        replier: FakeReplier | None = None,
        synthetic: bool = False,
    ) -> Message:
        attachments = []
        for name, data in files or []:
            kind = "photo" if name.lower().endswith((".png", ".jpg", ".jpeg")) else "document"
            cls = PhotoSize if kind == "photo" else Document
            attachments.append(
                (
                    kind,
                    cls(
                        file_id=f"https://cdn.example/{name}",
                        file_unique_id=name,
                        file_name=name,
                        file_size=len(data),
                        attachment=FakeAttachment(data),
                    ),
                )
            )
        message = Message(
            message_id=self.bot.next_id(),
            chat=chat,
            bot=self.bot,
            from_user=user,
            content=text,
            attachments=attachments,
            message_thread_id=thread_id,
            replier=replier,
            synthetic=synthetic,
            editable=False,
        )
        if not synthetic:
            self.bot.typed[message.message_id] = message
        return message

    async def dm(self, user: User, text: str | None = None, *, files: list[tuple[str, bytes]] | None = None) -> Message:
        message = self._message(user, text, chat=Chat(id=user.id, type="private"), files=files)
        await self.feed(Update(update_id=self.dp.next_update_id(), message=message))
        return message

    async def command(self, user: User, text: str, *, channel: int | None = None, thread_id: int | None = None) -> FakeReplier:
        """A slash command. No channel → the user's DMs; a staff channel (+thread) → answered there."""
        chat = Chat(id=channel, type="supergroup") if channel else Chat(id=user.id, type="private")
        replier = FakeReplier(self, kind="command", user_id=user.id, chat=chat, thread_id=thread_id)
        message = self._message(user, text, chat=chat, thread_id=thread_id, replier=replier, synthetic=True)
        await self.feed(Update(update_id=self.dp.next_update_id(), message=message))
        return replier

    async def staff_says(self, user: User, channel: int, thread_id: int | None, text: str | None = None,
                         *, files: list[tuple[str, bytes]] | None = None) -> Message:
        message = self._message(user, text, chat=Chat(id=channel, type="supergroup"), thread_id=thread_id, files=files)
        await self.feed(Update(update_id=self.dp.next_update_id(), message=message))
        return message

    async def press(self, user: User, label: str, *, on: Sent | None = None, exact: bool = False) -> Sent:
        """Press the button whose label contains `label` on `on` (default: the user's latest DM screen)."""
        target = on or self.latest(user.id)
        assert target is not None, "no screen to press on"
        matches = [
            b for b in target.buttons() if (b.text == label if exact else label in b.text)
        ]
        assert matches, f"no button {label!r} on screen; have {target.labels()}"
        button = matches[0]
        assert button.url is None, "that is a link button"
        if button.copy_text is not None:
            self.notices.append(Notice(user.id, button.copy_text.text, False))
            return target
        assert button.callback_data and button.callback_data != "noop", f"{label!r} is not pressable"
        await self.click(user, button.callback_data, on=target)
        return target

    async def click(self, user: User, data: str, *, on: Sent) -> None:
        chat = self.bot._chat(on.chat_id)
        replier = FakeReplier(self, kind="component", user_id=user.id, target=on, chat=chat, thread_id=on.thread_id)
        holder = Message(
            message_id=on.message_id,
            chat=chat,
            bot=self.bot,
            from_user=User(id=self.bot.id, is_bot=True, first_name="PowerX"),
            message_thread_id=on.thread_id,
            replier=replier,
            interaction_message=True,
            editable=on.kind == "screen",
        )
        query = CallbackQuery(id=str(self.bot.next_id()), from_user=user, data=data, message=holder, bot=self.bot, replier=replier)
        await self.feed(Update(update_id=self.dp.next_update_id(), callback_query=query))

    # -- reading ------------------------------------------------------------------------------

    def latest(self, chat_id: int, thread_id: int | None = None) -> Sent | None:
        screens = [s for s in self.bot.in_chat(chat_id, thread_id) if s.kind == "screen"]
        return screens[-1] if screens else None

    def texts(self, chat_id: int, thread_id: int | None = None) -> list[str]:
        return [s.text for s in self.bot.in_chat(chat_id, thread_id)]

    def last_notice(self, user_id: int) -> Notice | None:
        mine = [n for n in self.notices if n.user_id == user_id]
        return mine[-1] if mine else None


def as_bytes(text: str) -> bytes:
    return io.BytesIO(text.encode()).getvalue()


__all__ = ["FakeBot", "FakeReplier", "Harness", "Notice", "Screen", "Sent", "Thread", "as_bytes", "field"]
