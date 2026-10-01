"""The objects handlers see, shaped like aiogram's so the ported handlers read the same.

Mapping to Discord:
  * a DM with the bot is a `private` chat whose id is the user's id — exactly as on Telegram, where a
    private chat's id is the user's id. The Bot facade resolves it to the DM channel;
  * a staff channel (support / orders) is a `supergroup` chat; a thread in it is a forum *topic*,
    carried as `message_thread_id`;
  * an inline button's `callback_data` is a component `custom_id`; pressing it is a CallbackQuery;
  * a slash command is a Message whose text is `/<name> <args>`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.bot.framework.exceptions import BotBadRequest

if TYPE_CHECKING:
    import discord

    from app.bot.framework.bot import Bot
    from app.bot.framework.interaction import InteractionReplier


class BotObject:
    """Base class of everything a handler can receive (Telegram's `TelegramObject`)."""


# ---- keyboards ------------------------------------------------------------------------------


@dataclass
class CopyTextButton:
    text: str


@dataclass(kw_only=True)
class InlineKeyboardButton(BotObject):
    text: str
    callback_data: str | None = None
    url: str | None = None
    copy_text: CopyTextButton | None = None
    style: str | None = None
    # Discord-only: a button alone on its row with a description gets the description written
    # just above it, and a line between it and the next such button.
    description: str | None = None


@dataclass(kw_only=True)
class InlineKeyboardMarkup(BotObject):
    inline_keyboard: list[list[InlineKeyboardButton]] = field(default_factory=list)


@dataclass
class ReplyKeyboardRemove(BotObject):
    """Telegram's bottom-panel removal. Discord has no reply keyboard, so this renders as nothing."""

    remove_keyboard: bool = True


@dataclass
class LinkPreviewOptions(BotObject):
    is_disabled: bool | None = None


@dataclass
class BotCommand(BotObject):
    command: str
    description: str


@dataclass
class BotCommandScopeChat(BotObject):
    chat_id: int


@dataclass
class BufferedInputFile(BotObject):
    file: bytes
    filename: str


@dataclass
class ForumTopic(BotObject):
    message_thread_id: int
    name: str = ""


@dataclass
class MessageId(BotObject):
    message_id: int


# ---- identities -----------------------------------------------------------------------------


@dataclass
class User(BotObject):
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @classmethod
    def from_discord(cls, user: discord.abc.User) -> User:
        display = getattr(user, "global_name", None) or getattr(user, "display_name", None) or user.name
        return cls(id=user.id, is_bot=bool(user.bot), first_name=display or "", username=user.name)


@dataclass
class Chat(BotObject):
    id: int
    type: str  # "private" | "supergroup"
    title: str | None = None

    @property
    def full_name(self) -> str:
        return self.title or str(self.id)


# ---- media ----------------------------------------------------------------------------------


@dataclass
class _File(BotObject):
    """An attachment. `file_id` is its CDN URL, which is what the Bot facade downloads to re-send it."""

    file_id: str
    file_unique_id: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    attachment: Any = field(default=None, repr=False, compare=False)


class PhotoSize(_File):
    pass


class Document(_File):
    pass


class Video(_File):
    pass


class Audio(_File):
    pass


class Voice(_File):
    pass


class Animation(_File):
    pass


class VideoNote(_File):
    pass


@dataclass
class Sticker(BotObject):
    file_id: str
    name: str = ""
    sticker: Any = field(default=None, repr=False, compare=False)


def classify_attachment(attachment: discord.Attachment) -> tuple[str, _File]:
    """Which Telegram media kind an attachment corresponds to."""
    content_type = (attachment.content_type or "").lower()
    kwargs: dict[str, Any] = {
        "file_id": attachment.url,
        "file_unique_id": str(attachment.id),
        "file_name": attachment.filename,
        "mime_type": attachment.content_type,
        "file_size": attachment.size,
        "width": attachment.width,
        "height": attachment.height,
        "duration": getattr(attachment, "duration", None),
        "attachment": attachment,
    }
    if attachment.is_voice_message():
        return "voice", Voice(**kwargs)
    if content_type == "image/gif":
        return "animation", Animation(**kwargs)
    if content_type.startswith("image/"):
        return "photo", PhotoSize(**kwargs)
    if content_type.startswith("video/"):
        return "video", Video(**kwargs)
    if content_type.startswith("audio/"):
        return "audio", Audio(**kwargs)
    return "document", Document(**kwargs)


# ---- messages -------------------------------------------------------------------------------


_KEEP = object()


class Message(BotObject):
    def __init__(
        self,
        *,
        message_id: int,
        chat: Chat,
        bot: Bot | None = None,
        from_user: User | None = None,
        content: str | None = None,
        attachments: list[tuple[str, _File]] | None = None,
        stickers: list[Sticker] | None = None,
        message_thread_id: int | None = None,
        date: datetime | None = None,
        sender_chat: Chat | None = None,
        replier: InteractionReplier | None = None,
        interaction_message: bool = False,
        editable: bool = True,
        synthetic: bool = False,
        discord_message: discord.Message | None = None,
    ) -> None:
        self.message_id = message_id
        self.chat = chat
        self.bot = bot
        self.from_user = from_user
        self.message_thread_id = message_thread_id
        self.date = date or datetime.now(UTC)
        self.sender_chat = sender_chat
        self._replier = replier
        # This object IS the message a pressed component lives on, so editing it answers the
        # interaction instead of calling the edit endpoint.
        self._interaction_message = interaction_message
        # A Components V2 screen without media. Anything else (a relayed photo, a copied broadcast)
        # is refused by edit_text exactly as Telegram refuses edit_text on a media message.
        self._editable = editable
        # A slash command has no message of its own to reply to or delete.
        self._synthetic = synthetic
        self.discord_message = discord_message

        self.attachments: list[tuple[str, _File]] = list(attachments or [])
        self.stickers: list[Sticker] = list(stickers or [])
        has_media = bool(self.attachments or self.stickers)
        clean = content if content else None
        # Telegram semantics: a media message has a caption, not text.
        self.text: str | None = None if has_media else clean
        self.caption: str | None = clean if has_media else None

        kinds: dict[str, list[_File]] = {}
        for kind, item in self.attachments:
            kinds.setdefault(kind, []).append(item)
        self.photo: list[PhotoSize] | None = kinds.get("photo")  # type: ignore[assignment]
        self.video: Video | None = (kinds.get("video") or [None])[0]  # type: ignore[assignment]
        self.audio: Audio | None = (kinds.get("audio") or [None])[0]  # type: ignore[assignment]
        self.voice: Voice | None = (kinds.get("voice") or [None])[0]  # type: ignore[assignment]
        self.animation: Animation | None = (kinds.get("animation") or [None])[0]  # type: ignore[assignment]
        self.document: Document | None = (kinds.get("document") or [None])[0]  # type: ignore[assignment]
        self.video_note: VideoNote | None = None
        self.sticker: Sticker | None = self.stickers[0] if self.stickers else None

    # -- content ------------------------------------------------------------------------------

    @property
    def html_text(self) -> str:
        """The text as the sender formatted it, ready to be sent back out.

        Discord text is already markdown, so it is escaped for the HTML templates and wrapped in
        `<md>` — which the renderer passes through verbatim, formatting and all.
        """
        raw = self.text or self.caption or ""
        if not raw:
            return ""
        escaped = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<md>{escaped}</md>"

    @property
    def is_topic_message(self) -> bool:
        return self.message_thread_id is not None

    def _require_bot(self) -> Bot:
        if self.bot is None:
            raise RuntimeError("Message is not bound to a bot")
        return self.bot

    # -- actions ------------------------------------------------------------------------------

    async def answer(
        self,
        text: str,
        reply_markup: Any = None,
        *,
        parse_mode: str | None | object = _KEEP,
        **kwargs: Any,
    ) -> Message:
        bot = self._require_bot()
        mode = "HTML" if parse_mode is _KEEP else parse_mode
        if self._replier is not None and self._replier.answers_in_place:
            sent = await self._replier.send_first(bot.render(text, reply_markup, parse_mode=mode))
            if sent is not None:
                return bot.wrap_sent(sent, self.chat, self.message_thread_id)
        return await bot.send_message(
            self.chat.id,
            text,
            reply_markup=reply_markup,
            message_thread_id=self.message_thread_id,
            parse_mode=mode,
        )

    async def reply(
        self,
        text: str,
        reply_markup: Any = None,
        *,
        parse_mode: str | None | object = _KEEP,
        **kwargs: Any,
    ) -> Message:
        bot = self._require_bot()
        mode = "HTML" if parse_mode is _KEEP else parse_mode
        if self._replier is not None and self._replier.answers_in_place:
            sent = await self._replier.send_first(bot.render(text, reply_markup, parse_mode=mode))
            if sent is not None:
                return bot.wrap_sent(sent, self.chat, self.message_thread_id)
        return await bot.send_message(
            self.chat.id,
            text,
            reply_markup=reply_markup,
            message_thread_id=self.message_thread_id,
            parse_mode=mode,
            reply_to_message_id=None if self._synthetic else self.message_id,
        )

    async def edit_text(
        self,
        text: str,
        reply_markup: Any = None,
        *,
        parse_mode: str | None | object = _KEEP,
        **kwargs: Any,
    ) -> Message:
        bot = self._require_bot()
        mode = "HTML" if parse_mode is _KEEP else parse_mode
        if not self._editable:
            raise BotBadRequest("Bad Request: there is no text in the message to edit")
        if self._interaction_message and self._replier is not None:
            await self._replier.edit_component_message(bot.render(text, reply_markup, parse_mode=mode))
            return self
        return await bot.edit_message_text(
            text,
            chat_id=self.chat.id,
            message_id=self.message_id,
            message_thread_id=self.message_thread_id,
            reply_markup=reply_markup,
            parse_mode=mode,
        )

    async def delete(self) -> bool:
        if self._synthetic:
            return False
        return await self._require_bot().delete_message(
            self.chat.id, self.message_id, message_thread_id=self.message_thread_id
        )

    async def answer_document(
        self, document: Any, caption: str | None = None, reply_markup: Any = None, **kwargs: Any
    ) -> Message:
        return await self._require_bot().send_document(
            self.chat.id,
            document,
            caption=caption,
            reply_markup=reply_markup,
            message_thread_id=self.message_thread_id,
        )

    def __repr__(self) -> str:
        return (
            f"<Message id={self.message_id} chat={self.chat.id}/{self.chat.type} "
            f"thread={self.message_thread_id} text={self.text!r}>"
        )


class CallbackQuery(BotObject):
    def __init__(
        self,
        *,
        id: str,
        from_user: User,
        data: str | None,
        message: Message | None,
        bot: Bot | None = None,
        replier: InteractionReplier | None = None,
    ) -> None:
        self.id = id
        self.from_user = from_user
        self.data = data
        self.message = message
        self.bot = bot
        self.chat_instance = ""
        self._replier = replier

    async def answer(self, text: str | None = None, show_alert: bool = False, **kwargs: Any) -> bool:
        if self._replier is None:
            return True
        if text:
            await self._replier.notify(text, alert=show_alert)
        else:
            await self._replier.ack()
        return True

    def __repr__(self) -> str:
        return f"<CallbackQuery data={self.data!r} from={self.from_user.id}>"


class Update(BotObject):
    def __init__(
        self,
        *,
        update_id: int,
        message: Message | None = None,
        callback_query: CallbackQuery | None = None,
    ) -> None:
        self.update_id = update_id
        self.message = message
        self.callback_query = callback_query

    @property
    def event_type(self) -> str:
        if self.message is not None:
            return "message"
        if self.callback_query is not None:
            return "callback_query"
        return "unknown"

    @property
    def event(self) -> Message | CallbackQuery | None:
        return self.message or self.callback_query


def as_bytes_io(data: bytes) -> io.BytesIO:
    buffer = io.BytesIO(data)
    buffer.seek(0)
    return buffer


__all__ = [
    "Animation",
    "Audio",
    "BotCommand",
    "BotCommandScopeChat",
    "BotObject",
    "BufferedInputFile",
    "CallbackQuery",
    "Chat",
    "CopyTextButton",
    "Document",
    "ForumTopic",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "LinkPreviewOptions",
    "Message",
    "MessageId",
    "PhotoSize",
    "ReplyKeyboardRemove",
    "Sticker",
    "Update",
    "User",
    "Video",
    "VideoNote",
    "Voice",
    "classify_attachment",
]
