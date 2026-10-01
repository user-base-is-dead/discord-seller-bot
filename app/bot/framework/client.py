"""The bridge from discord.py events to the dispatcher.

What reaches the handlers, and as what:

  DM from a user                      → Message in a `private` chat (chat id = user id)
  message in a staff channel/thread   → Message in a `supergroup` chat (chat id = the staff channel,
                                        message_thread_id = the thread)
  button press / dropdown choice      → CallbackQuery whose data is the button's callback_data
  slash command                       → Message whose text is "/<name> <args>"

Anything else in a guild is none of the store's business and is ignored, the way the Telegram bot
never saw chats it was not in.

A slash command used in an ordinary server channel is answered in the user's DMs — the store is a
private conversation on Telegram, and a wallet or an order has no business being posted in a public
channel. Used in a staff channel or thread, it is answered right there, like a command in the staff
group was.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional

import discord
from discord import app_commands

from app.bot.framework.bot import Bot
from app.bot.framework.interaction import InteractionReplier, auto_ack
from app.bot.framework.render import COPY_PREFIX, SELECT_PREFIX, copy_value, strip_dedupe
from app.bot.framework.router import Dispatcher
from app.bot.framework.types import (
    CallbackQuery,
    Chat,
    Message,
    Sticker,
    Update,
    User,
    classify_attachment,
)

logger = logging.getLogger(__name__)

OPEN_STORE = "open_store"

_DM_POINTER = (
    "📬 I've opened this in your DMs with me.\n"
    "Nothing there? Allow direct messages from this server (Server menu → Privacy Settings), "
    "then try again."
)
_OPEN_STORE_POINTER = "📬 The store is open in your DMs with me — check your messages."
_GUILD_BUTTON_NOTE = "🔒 That button only works in your DMs with me. Use /start to open the store."

_USER_MESSAGE_TYPES = {discord.MessageType.default, discord.MessageType.reply}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    takes_args: bool = False
    args_hint: str = "Arguments"
    admin: bool = False


@dataclass(frozen=True)
class _Context:
    kind: str  # "private" | "staff" | "guild"
    chat_id: int
    thread_id: int | None = None
    title: str | None = None


class StoreClient(discord.Client):
    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        staff_channel_ids: Iterable[int | None],
        known_channel_ids: Iterable[int | None] = (),
        commands: Iterable[CommandSpec] = (),
        accent_colour: int | None = None,
        button_width: float | None = None,
        sync_commands: bool = True,
        **options: Any,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        # Privileged: needed to read what staff type in ticket/order threads (DM content is always
        # delivered). Enable "Message Content Intent" for the bot in the Developer Portal.
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), **options)

        self.dp = dispatcher
        self.staff_channel_ids = {int(c) for c in staff_channel_ids if c}
        self.bot = Bot(
            self,
            accent_colour=accent_colour,
            known_channels=self.staff_channel_ids | {int(c) for c in known_channel_ids if c},
            **({"button_width": button_width} if button_width else {}),
        )
        self.tree = app_commands.CommandTree(self)
        self._sync_commands = sync_commands
        self._ready_once: list[Callable[[], Awaitable[Any]]] = []
        self._ready_fired = False
        self._resolved: dict[int, Any] = {}
        for spec in commands:
            self.tree.add_command(self._build_command(spec))

    # -- lifecycle ----------------------------------------------------------------------------

    def on_first_ready(self, callback: Callable[[], Awaitable[Any]]) -> None:
        self._ready_once.append(callback)

    async def setup_hook(self) -> None:
        if self._sync_commands:
            try:
                synced = await self.tree.sync()
                logger.info("Synced %d slash command(s)", len(synced))
            except discord.HTTPException as exc:
                logger.error("Slash command sync failed: %s", exc)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id %s)", self.user, self.user.id if self.user else "?")
        if self._ready_fired:
            return
        self._ready_fired = True
        for callback in self._ready_once:
            try:
                await callback()
            except Exception:
                logger.exception("Startup task failed")

    # -- context ------------------------------------------------------------------------------

    def _context(self, channel: Any, guild_id: int | None) -> _Context:
        if guild_id is None or isinstance(channel, discord.DMChannel):
            return _Context("private", 0)
        if isinstance(channel, discord.Thread):
            if channel.parent_id in self.staff_channel_ids:
                return _Context("staff", channel.parent_id, channel.id, getattr(channel.parent, "name", None))
            return _Context("guild", channel.id)
        channel_id = getattr(channel, "id", 0)
        if channel_id in self.staff_channel_ids:
            return _Context("staff", channel_id, None, getattr(channel, "name", None))
        return _Context("guild", channel_id)

    @staticmethod
    def _chat(context: _Context, user: User) -> Chat:
        if context.kind == "private":
            return Chat(id=user.id, type="private")
        return Chat(id=context.chat_id, type="supergroup", title=context.title)

    def _note_dm(self, context: _Context, user: User, channel: Any) -> None:
        channel_id = getattr(channel, "id", None)
        if context.kind == "private" and channel_id:
            self.bot.remember_dm(user.id, channel_id)

    # -- dispatch -----------------------------------------------------------------------------

    async def _dispatch(self, update: Update, replier: InteractionReplier | None = None) -> None:
        ack_task = asyncio.create_task(auto_ack(replier)) if replier is not None else None
        try:
            await self.dp.feed_update(self.bot, update)
        except Exception:
            logger.exception("Unhandled error while dispatching update %s", update.update_id)
        finally:
            if ack_task is not None:
                ack_task.cancel()
            if replier is not None:
                await replier.finalize()

    # -- messages -----------------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.type not in _USER_MESSAGE_TYPES:
            return
        guild_id = message.guild.id if message.guild else None
        channel = await self._resolve(message.channel, guild_id)
        context = self._context(channel, guild_id)
        if context.kind == "guild":
            return
        user = User.from_discord(message.author)
        self._note_dm(context, user, message.channel)
        shim = self.to_message(message, self._chat(context, user), context.thread_id)
        await self._dispatch(Update(update_id=self.dp.next_update_id(), message=shim))

    async def _resolve(self, channel: Any, guild_id: int | None) -> Any:
        """The real channel behind a message in a thread discord.py has not cached.

        Such a message arrives on a bare PartialMessageable with no parent, so nothing tells a staff
        thread from any other — and archived threads leave the cache, so a staff reply that revives
        one could otherwise be dropped without a word. Fetched once, then remembered.
        """
        if guild_id is None or not isinstance(channel, discord.PartialMessageable):
            return channel
        known = self._resolved.get(channel.id)
        if known is not None:
            return known
        try:
            fetched = await self.fetch_channel(channel.id)
        except discord.HTTPException as exc:
            logger.debug("Couldn't resolve channel %s (%s)", channel.id, exc)
            return channel
        if len(self._resolved) >= 1024:
            self._resolved.pop(next(iter(self._resolved)))
        self._resolved[channel.id] = fetched
        return fetched

    def to_message(self, message: discord.Message, chat: Chat, thread_id: int | None) -> Message:
        return Message(
            message_id=message.id,
            chat=chat,
            bot=self.bot,
            from_user=User.from_discord(message.author),
            content=message.content,
            attachments=[classify_attachment(a) for a in message.attachments],
            stickers=[Sticker(file_id=s.url, name=s.name, sticker=s) for s in message.stickers],
            message_thread_id=thread_id,
            date=message.created_at,
            # A webhook post has no person behind it to credit (Telegram's "on behalf of a chat").
            sender_chat=Chat(id=message.webhook_id, type="channel") if message.webhook_id else None,
            editable=False,
            discord_message=message,
        )

    # -- components ---------------------------------------------------------------------------

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return  # slash commands are routed by the command tree
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")

        if custom_id.startswith(COPY_PREFIX):
            value = copy_value(custom_id)
            # Sent bare, so a long-press → Copy Text on mobile takes exactly the value.
            await self._safe(interaction.response.send_message(value or "—", ephemeral=True))
            return

        if custom_id.startswith(SELECT_PREFIX):
            values = data.get("values") or []
            payload = str(values[0]) if values else ""
        else:
            payload = custom_id
        payload = strip_dedupe(payload)

        if payload == OPEN_STORE:
            await self._open_store(interaction)
            return

        context = self._context(interaction.channel, interaction.guild_id)
        if context.kind == "guild":
            await self._safe(interaction.response.send_message(_GUILD_BUTTON_NOTE, ephemeral=True))
            return

        user = User.from_discord(interaction.user)
        self._note_dm(context, user, interaction.channel)
        chat = self._chat(context, user)
        replier = InteractionReplier(interaction, kind="component")
        source = interaction.message
        editable = bool(
            source is not None and source.flags.components_v2 and not source.attachments
        )
        holder = Message(
            message_id=source.id if source is not None else 0,
            chat=chat,
            bot=self.bot,
            from_user=User.from_discord(source.author) if source is not None else None,
            message_thread_id=context.thread_id,
            replier=replier,
            interaction_message=True,
            editable=editable,
            discord_message=source,
        )
        query = CallbackQuery(
            id=str(interaction.id),
            from_user=user,
            data=payload,
            message=holder,
            bot=self.bot,
            replier=replier,
        )
        await self._dispatch(Update(update_id=self.dp.next_update_id(), callback_query=query), replier)

    async def _open_store(self, interaction: discord.Interaction) -> None:
        """The storefront button posted in a server channel: the store itself opens in DMs."""
        await self._safe(interaction.response.send_message(_OPEN_STORE_POINTER, ephemeral=True))
        user = User.from_discord(interaction.user)
        message = Message(
            message_id=interaction.id,
            chat=Chat(id=user.id, type="private"),
            bot=self.bot,
            from_user=user,
            content="/start",
            synthetic=True,
        )
        await self._dispatch(Update(update_id=self.dp.next_update_id(), message=message))

    # -- slash commands -----------------------------------------------------------------------

    def _build_command(self, spec: CommandSpec) -> app_commands.Command:
        client = self

        if spec.takes_args:

            async def callback(interaction: discord.Interaction, args: Optional[str] = None) -> None:
                await client._on_command(interaction, spec.name, args)

            callback = app_commands.describe(args=spec.args_hint[:100])(callback)
        else:

            async def callback(interaction: discord.Interaction) -> None:  # type: ignore[misc]
                await client._on_command(interaction, spec.name, None)

        command = app_commands.Command(
            name=spec.name,
            description=spec.description[:100],
            callback=callback,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )
        if spec.admin:
            # Hidden from ordinary server members; the handlers still check IsAdmin themselves.
            command.default_permissions = discord.Permissions(manage_guild=True)
        return command

    async def _on_command(self, interaction: discord.Interaction, name: str, args: str | None) -> None:
        text = f"/{name}" + (f" {args.strip()}" if args and args.strip() else "")
        user = User.from_discord(interaction.user)
        context = self._context(interaction.channel, interaction.guild_id)
        in_place = context.kind != "guild"
        replier = InteractionReplier(interaction, kind="command", answers_in_place=in_place)
        if not in_place:
            await self._safe(replier.send_pointer(_DM_POINTER))
            context = _Context("private", 0)
        else:
            self._note_dm(context, user, interaction.channel)
        message = Message(
            message_id=interaction.id,
            chat=self._chat(context, user),
            bot=self.bot,
            from_user=user,
            content=text,
            message_thread_id=context.thread_id,
            replier=replier,
            synthetic=True,
        )
        await self._dispatch(Update(update_id=self.dp.next_update_id(), message=message), replier)

    @staticmethod
    async def _safe(awaitable: Any) -> None:
        try:
            await awaitable
        except Exception as exc:
            logger.debug("Interaction reply failed: %s", exc)


__all__ = ["OPEN_STORE", "CommandSpec", "StoreClient"]
