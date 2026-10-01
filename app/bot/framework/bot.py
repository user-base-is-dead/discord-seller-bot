"""The `Bot` object handlers, services and jobs call — Telegram's Bot API, implemented on Discord.

Addressing works the way it did on Telegram:
  * `chat_id` of a private chat is the user's id → their DM channel;
  * `chat_id` of a staff channel plus `message_thread_id` → a thread (topic) in it;
  * a forum *topic* is a Discord thread: a post in a forum channel, or a public thread in a text
    channel. Closing a topic archives and locks the thread, reopening reverses it.

Screens (`send_message`, `edit_message_text`) are Components V2 cards. Media sends and message copies
are classic messages carrying files, because a V2 message cannot show an attachment it does not
reference by name.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import discord

from app.bot.framework.exceptions import BotAPIError, BotBadRequest, BotNotFound, translate
from app.bot.framework.markdown import CONTENT_LIMIT, TEXT_LIMIT, to_card, to_markdown
from app.bot.framework.render import DEFAULT_BUTTON_WIDTH, build_classic_view, build_layout, tone_colour
from app.bot.framework.types import (
    BufferedInputFile,
    Chat,
    ForumTopic,
    Message,
    MessageId,
    User,
    _File,
)

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
# Discord's default upload ceiling for a bot without a boosted server. A bigger file is linked
# instead of re-uploaded, so a relay never fails outright on size.
UPLOAD_LIMIT = 10 * 1024 * 1024
# A card's `<thumb src="bot">` is the store's own avatar; anything else must be a web address.
BOT_AVATAR = "bot"
_WEB_URL = re.compile(r"https?://", re.I)
# How long a buyer's avatar is trusted before it is looked up again.
_AVATAR_TTL = 3600.0


def _avatar_of(user: Any) -> str | None:
    """A user's avatar (or Discord's default one) at thumbnail size."""
    if user is None:
        return None
    try:
        return user.display_avatar.with_size(256).url
    except (AttributeError, ValueError):
        return None


class Bot:
    def __init__(
        self,
        client: discord.Client,
        *,
        accent_colour: int | None = None,
        known_channels: set[int] | None = None,
        button_width: float = DEFAULT_BUTTON_WIDTH,
    ) -> None:
        self.client = client
        self.accent_colour = accent_colour
        self.button_width = float(button_width)
        # Staff/log channels from the config. An id in here is never mistaken for a user id.
        self.known_channels: set[int] = {c for c in (known_channels or set()) if c}
        # user id → their DM channel id, so a DM to a known buyer is one request, not three.
        self._dm_channels: dict[int, int] = {}
        self._copy_cache: dict[tuple[int, int], tuple[float, str | None, list[tuple[str, bytes, bool]], list[str]]] = {}
        # user id → (looked up at, avatar url) for the buyer cards in staff threads.
        self._avatars: dict[int, tuple[float, str | None]] = {}

    def remember_dm(self, user_id: int, channel_id: int) -> None:
        if len(self._dm_channels) > 50_000:
            self._dm_channels.pop(next(iter(self._dm_channels)))
        self._dm_channels[int(user_id)] = int(channel_id)

    # -- identity -----------------------------------------------------------------------------

    @property
    def id(self) -> int:
        return self.client.user.id if self.client.user else 0

    async def get_me(self) -> User:
        if self.client.user is None:
            raise BotAPIError("Bot is not logged in")
        return User.from_discord(self.client.user)

    @property
    def avatar_url(self) -> str | None:
        """The store's own avatar: the logo on the welcome screen and the storefront."""
        return _avatar_of(getattr(self.client, "user", None))

    async def user_avatar_url(self, user_id: int) -> str | None:
        """A buyer's avatar for the cards in staff threads, or None when Discord won't say.

        Usually a cache hit — the buyer has just written to the bot — and remembered for an hour
        either way, so editing an order card on every step costs no extra requests.
        """
        client = self.client
        if client is None or not user_id:
            return None
        key = int(user_id)
        cached = self._avatars.get(key)
        if cached is not None and time.monotonic() - cached[0] < _AVATAR_TTL:
            return cached[1]
        user = client.get_user(key)
        if user is None:
            try:
                user = await client.fetch_user(key)
            except Exception as exc:  # cosmetic: no lookup failure may cost a ticket or an order card
                logger.debug("Couldn't look up user %s for an avatar (%s)", key, exc)
                return None
        url = _avatar_of(user)
        if len(self._avatars) >= 4096:
            self._avatars.pop(next(iter(self._avatars)))
        self._avatars[key] = (time.monotonic(), url)
        return url

    # -- rendering ----------------------------------------------------------------------------

    def render(self, text: str, reply_markup: Any = None, *, parse_mode: str | None = "HTML") -> discord.ui.LayoutView:
        markdown, thumbnail = to_card(text, parse_mode, limit=TEXT_LIMIT)
        accent = self.accent_colour
        if accent is not None:
            # A status card (✅ done, 🚫 refused, ⏳ waiting, 🔒 closed) wears its status colour;
            # everything else keeps the brand stripe. No stripe at all stays no stripe.
            accent = tone_colour(markdown) or accent
        return build_layout(
            markdown,
            reply_markup,
            accent_colour=accent,
            thumbnail=self._thumbnail(thumbnail),
            button_width=self.button_width,
        )

    def _thumbnail(self, source: str | None) -> str | None:
        if source == BOT_AVATAR:
            return self.avatar_url
        if source and _WEB_URL.match(source):
            return source
        return None

    def wrap_sent(self, sent: Any, chat: Chat, thread_id: int | None) -> Message:
        message_id = sent if isinstance(sent, int) else getattr(sent, "id", 0)
        return Message(
            message_id=message_id,
            chat=chat,
            bot=self,
            from_user=User.from_discord(self.client.user) if self.client.user else None,
            message_thread_id=thread_id,
            discord_message=sent if isinstance(sent, discord.Message) else None,
        )

    # -- addressing ---------------------------------------------------------------------------

    async def channel_for(self, chat_id: int, thread_id: int | None = None) -> discord.abc.Messageable:
        target = int(thread_id or chat_id)
        channel = self.client.get_channel(target)
        if channel is not None:
            return channel  # type: ignore[return-value]

        # Most sends without a thread are DMs to buyers; staff channels are listed in known_channels.
        if thread_id is None and target not in self.known_channels:
            dm_id = self._dm_channels.get(target)
            if dm_id is not None:
                return self.client.get_channel(dm_id) or self.client.get_partial_messageable(
                    dm_id, type=discord.ChannelType.private
                )
            user = self.client.get_user(target)
            if user is None:
                try:
                    user = await self.client.fetch_user(target)
                except discord.NotFound:
                    user = None
                except discord.HTTPException as exc:
                    raise translate(exc) from exc
            if user is not None:
                if user.dm_channel is not None:
                    self.remember_dm(target, user.dm_channel.id)
                    return user.dm_channel
                try:
                    channel = await user.create_dm()
                except discord.HTTPException as exc:
                    raise translate(exc) from exc
                self.remember_dm(target, channel.id)
                return channel
        try:
            fetched = await self.client.fetch_channel(target)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return fetched  # type: ignore[return-value]

    def chat_for(self, channel: Any, chat_id: int, thread_id: int | None) -> Chat:
        is_dm = isinstance(channel, discord.DMChannel) or getattr(channel, "type", None) == discord.ChannelType.private
        if is_dm:
            return Chat(id=chat_id, type="private")
        return Chat(id=chat_id, type="supergroup", title=getattr(channel, "name", None))

    async def _partial(self, chat_id: int, message_id: int, thread_id: int | None) -> discord.PartialMessage:
        channel = await self.channel_for(chat_id, thread_id)
        get_partial = getattr(channel, "get_partial_message", None)
        if get_partial is None:
            raise BotBadRequest("Bad Request: chat has no messages")
        return get_partial(int(message_id))

    # -- screens ------------------------------------------------------------------------------

    async def send_message(
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
        channel = await self.channel_for(chat_id, message_thread_id)
        view = self.render(text, reply_markup, parse_mode=parse_mode)
        reference = None
        if reply_to_message_id and hasattr(channel, "get_partial_message"):
            reference = channel.get_partial_message(int(reply_to_message_id)).to_reference(fail_if_not_exists=False)
        try:
            sent = await channel.send(view=view, allowed_mentions=NO_MENTIONS, reference=reference)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return self._wrap(sent, chat_id, message_thread_id)

    async def edit_message_text(
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
        if chat_id is None or message_id is None:
            raise BotBadRequest("Bad Request: chat_id and message_id are required")
        partial = await self._partial(chat_id, message_id, message_thread_id)
        view = self.render(text, reply_markup, parse_mode=parse_mode)
        try:
            edited = await partial.edit(view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return self._wrap(edited, chat_id, message_thread_id)

    async def delete_message(self, chat_id: int, message_id: int, *, message_thread_id: int | None = None) -> bool:
        partial = await self._partial(chat_id, message_id, message_thread_id)
        try:
            await partial.delete()
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return True

    def _wrap(self, sent: discord.Message, chat_id: int, thread_id: int | None) -> Message:
        chat = self.chat_for(sent.channel, chat_id, thread_id)
        return Message(
            message_id=sent.id,
            chat=chat,
            bot=self,
            from_user=User.from_discord(sent.author),
            message_thread_id=thread_id,
            discord_message=sent,
        )

    # -- topics (threads) ---------------------------------------------------------------------

    async def create_forum_topic(self, chat_id: int, name: str, *, opening: str | None = None, **_: Any) -> ForumTopic:
        """Open a thread. In a forum, `opening` (HTML) is the post's first message — the line staff
        read in the channel's post list before they open the thread — and defaults to the name."""
        channel = await self.channel_for(chat_id)
        name = (name or "topic").strip()[:100]
        try:
            if isinstance(channel, discord.ForumChannel):
                extra: dict[str, Any] = {}
                # A forum set to "require a tag" refuses untagged posts outright; the first tag
                # keeps tickets flowing, and staff can re-tag the post by hand.
                if channel.flags.require_tag and channel.available_tags:
                    extra["applied_tags"] = [channel.available_tags[0]]
                content = (
                    to_markdown(opening, "HTML", limit=CONTENT_LIMIT)
                    if opening
                    else f"🧵 **{discord.utils.escape_markdown(name)}**"
                )
                created = await channel.create_thread(
                    name=name,
                    content=content,
                    allowed_mentions=NO_MENTIONS,
                    **extra,
                )
                thread = created.thread
            elif isinstance(channel, discord.TextChannel):
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread, auto_archive_duration=10080
                )
            else:
                raise BotBadRequest("Bad Request: the chat is not a forum or text channel")
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return ForumTopic(message_thread_id=thread.id, name=thread.name)

    async def _thread(self, chat_id: int, thread_id: int) -> discord.Thread:
        channel = await self.channel_for(chat_id, thread_id)
        if not isinstance(channel, discord.Thread):
            raise BotBadRequest("Bad Request: message thread not found")
        return channel

    async def close_forum_topic(self, chat_id: int, message_thread_id: int, **_: Any) -> bool:
        thread = await self._thread(chat_id, message_thread_id)
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return True

    async def reopen_forum_topic(self, chat_id: int, message_thread_id: int, **_: Any) -> bool:
        thread = await self._thread(chat_id, message_thread_id)
        try:
            await thread.edit(archived=False, locked=False)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return True

    # -- files --------------------------------------------------------------------------------

    async def _read(self, source: Any) -> tuple[bytes, str]:
        if isinstance(source, BufferedInputFile):
            return source.file, source.filename
        if isinstance(source, _File):
            if source.attachment is not None:
                try:
                    return await source.attachment.read(), source.file_name or "file"
                except discord.HTTPException as exc:
                    raise translate(exc) from exc
            return await self._read(source.file_id)
        if isinstance(source, str):
            name = urlparse(source).path.rsplit("/", 1)[-1] or "file"
            try:
                data = await self.client.http.get_from_cdn(source)
            except discord.HTTPException as exc:
                raise translate(exc) from exc
            return data, name
        raise BotBadRequest(f"Bad Request: unsupported file {type(source).__name__}")

    async def download(self, file: Any, destination: Any = None, **_: Any) -> io.BytesIO:
        data, _name = await self._read(file)
        buffer = destination if destination is not None else io.BytesIO()
        buffer.write(data)
        buffer.seek(0)
        return buffer

    async def _send_file(
        self,
        chat_id: int,
        source: Any,
        *,
        caption: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: Any = None,
        parse_mode: str | None = "HTML",
    ) -> Message:
        channel = await self.channel_for(chat_id, message_thread_id)
        data, name = await self._read(source)
        content = to_markdown(caption, parse_mode, limit=CONTENT_LIMIT) if caption else None
        view = build_classic_view(reply_markup, button_width=self.button_width)
        kwargs: dict[str, Any] = {"allowed_mentions": NO_MENTIONS}
        if view is not None:
            kwargs["view"] = view
        try:
            if len(data) > UPLOAD_LIMIT and isinstance(source, (str, _File)):
                link = source if isinstance(source, str) else source.file_id
                sent = await channel.send(content=((content or "") + f"\n📎 {name}: {link}").strip(), **kwargs)
            else:
                sent = await channel.send(content=content, file=discord.File(io.BytesIO(data), filename=name), **kwargs)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return self._wrap(sent, chat_id, message_thread_id)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, document, **_file_kwargs(kwargs))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, photo, **_file_kwargs(kwargs))

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, video, **_file_kwargs(kwargs))

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, audio, **_file_kwargs(kwargs))

    async def send_voice(self, chat_id: int, voice: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, voice, **_file_kwargs(kwargs))

    async def send_video_note(self, chat_id: int, video_note: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, video_note, **_file_kwargs(kwargs))

    async def send_animation(self, chat_id: int, animation: Any, **kwargs: Any) -> Message:
        return await self._send_file(chat_id, animation, **_file_kwargs(kwargs))

    # -- copies -------------------------------------------------------------------------------

    async def _source(self, from_chat_id: int, message_id: int, thread_id: int | None):
        key = (int(from_chat_id), int(message_id))
        cached = self._copy_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < 600:
            return cached[1], cached[2], cached[3]
        channel = await self.channel_for(from_chat_id, thread_id)
        try:
            source = await channel.fetch_message(int(message_id))
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        files: list[tuple[str, bytes, bool]] = []
        links: list[str] = []
        for attachment in source.attachments:
            if attachment.size > UPLOAD_LIMIT:
                links.append(f"📎 {attachment.filename}: {attachment.url}")
                continue
            try:
                files.append((attachment.filename, await attachment.read(), attachment.is_spoiler()))
            except discord.HTTPException as exc:
                logger.warning("Couldn't read attachment %s for copy (%s)", attachment.id, exc)
                links.append(f"📎 {attachment.filename}: {attachment.url}")
        content = source.content or None
        if source.stickers and not content and not files:
            links.append(f"🌟 {source.stickers[0].name}")
        if len(self._copy_cache) > 32:
            self._copy_cache.pop(next(iter(self._copy_cache)))
        self._copy_cache[key] = (time.monotonic(), content, files, links)
        return content, files, links

    async def copy_message(
        self,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        *,
        reply_markup: Any = None,
        message_thread_id: int | None = None,
        from_message_thread_id: int | None = None,
        **_: Any,
    ) -> MessageId:
        """Re-send somebody's message as the bot — text exactly as written, files re-uploaded."""
        content, files, links = await self._source(from_chat_id, message_id, from_message_thread_id)
        body = "\n".join(part for part in [content or "", *links] if part) or None
        if body and len(body) > CONTENT_LIMIT:
            body = body[: CONTENT_LIMIT - 1] + "…"
        target = await self.channel_for(chat_id, message_thread_id)
        view = build_classic_view(reply_markup, button_width=self.button_width)
        kwargs: dict[str, Any] = {"allowed_mentions": NO_MENTIONS}
        if view is not None:
            kwargs["view"] = view
        if files:
            kwargs["files"] = [
                discord.File(io.BytesIO(data), filename=name, spoiler=spoiler) for name, data, spoiler in files
            ]
        if body is None and not files:
            body = "\u200b"
        try:
            sent = await target.send(content=body, **kwargs)
        except discord.HTTPException as exc:
            raise translate(exc) from exc
        return MessageId(message_id=sent.id)

    # -- lifecycle ----------------------------------------------------------------------------

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()


def _file_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = ("caption", "message_thread_id", "reply_markup", "parse_mode")
    return {k: v for k, v in kwargs.items() if k in allowed}


__all__ = ["BOT_AVATAR", "NO_MENTIONS", "Bot", "BotNotFound"]
