"""Answering one Discord interaction the way an aiogram handler asked to.

Telegram and Discord disagree about acknowledgement. A Telegram callback query may be answered late
or never; a Discord interaction must be answered within three seconds, exactly once, and every later
change goes through its webhook. This class holds that bookkeeping so handlers don't have to:

  * `edit_component_message` — `query.message.edit_text(...)`: updates the message the button is on;
  * `notify`                 — `query.answer("…", show_alert=…)`: an ephemeral note (a toast fades,
                               an alert stays until dismissed);
  * `ack`                    — `query.answer()`: silent acknowledgement;
  * `send_first`             — a slash command's first `message.answer(...)` becomes the command's
                               own response; later answers are ordinary channel messages.

A background timer calls `ack` if the handler is still busy near the deadline, and `finalize`
acknowledges whatever the handler never answered — so no press ends in "This interaction failed".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import discord

from app.bot.framework.exceptions import BotAPIError, translate
from app.bot.framework.markdown import CONTENT_LIMIT, plain_to_markdown

logger = logging.getLogger(__name__)

ACK_AFTER_SECONDS = 1.8
TOAST_SECONDS = 8.0


class InteractionReplier:
    def __init__(
        self,
        interaction: discord.Interaction,
        *,
        kind: str,
        answers_in_place: bool = True,
    ) -> None:
        self.interaction = interaction
        self.kind = kind  # "component" | "command"
        # A slash command run outside the store's own chat is answered in the user's DMs instead;
        # its interaction then only carries the "check your DMs" pointer.
        self.answers_in_place = kind == "command" and answers_in_place
        self._lock = asyncio.Lock()
        self._mode: str | None = None  # "update" | "message" | "thinking"
        self._original_consumed = False

    @property
    def done(self) -> bool:
        return self.interaction.response.is_done()

    @staticmethod
    async def _call(awaitable: Any) -> Any:
        try:
            return await awaitable
        except discord.HTTPException as exc:
            raise translate(exc) from exc

    # -- intents ------------------------------------------------------------------------------

    async def ack(self) -> None:
        async with self._lock:
            if self.done:
                return
            thinking = self.kind == "command"
            await self._call(self.interaction.response.defer(thinking=thinking))
            self._mode = "thinking" if thinking else "update"

    async def edit_component_message(self, view: discord.ui.LayoutView) -> None:
        async with self._lock:
            response = self.interaction.response
            if not response.is_done():
                await self._call(response.edit_message(view=view))
                self._mode = "update"
            elif self._mode == "update":
                await self._call(self.interaction.edit_original_response(view=view))
            elif self.interaction.message is not None:
                # The interaction's own response is an ephemeral note, so "@original" would be that
                # note. The component's message is edited directly instead.
                await self._call(self.interaction.message.edit(view=view))
            else:
                raise BotAPIError("No message to edit for this interaction")

    async def notify(self, text: str, *, alert: bool) -> None:
        content = plain_to_markdown(text, limit=CONTENT_LIMIT)
        delete_after = None if alert else TOAST_SECONDS
        async with self._lock:
            response = self.interaction.response
            if not response.is_done():
                await self._call(response.send_message(content, ephemeral=True, delete_after=delete_after))
                self._mode = self._mode or "message"
                return
            sent = await self._call(self.interaction.followup.send(content, ephemeral=True, wait=True))
        if delete_after and sent is not None:
            asyncio.get_running_loop().create_task(_delete_later(sent, delete_after))

    async def send_first(self, view: discord.ui.LayoutView) -> Any:
        """Use the command's own response slot for its first answer. None when it is already spent."""
        if not self.answers_in_place:
            return None
        async with self._lock:
            response = self.interaction.response
            if not response.is_done():
                result = await self._call(response.send_message(view=view))
                self._mode = "message"
                self._original_consumed = True
                message_id = getattr(result, "message_id", None)
                if message_id is None:
                    with contextlib.suppress(discord.HTTPException):
                        return await self.interaction.original_response()
                return message_id
            if self._mode == "thinking" and not self._original_consumed:
                self._original_consumed = True
                return await self._call(self.interaction.edit_original_response(view=view))
            return None

    async def send_pointer(self, content: str) -> None:
        """The ephemeral "sent to your DMs" note for a command answered elsewhere."""
        async with self._lock:
            if not self.done:
                await self._call(self.interaction.response.send_message(content, ephemeral=True))
                self._mode = "message"

    async def finalize(self) -> None:
        """Acknowledge whatever the handler left unanswered."""
        async with self._lock:
            response = self.interaction.response
            try:
                if not response.is_done():
                    if self.kind == "component":
                        await response.defer()
                    else:
                        await response.send_message("✅", ephemeral=True, delete_after=3)
                elif self._mode == "thinking" and not self._original_consumed:
                    await self.interaction.delete_original_response()
            except discord.HTTPException as exc:
                logger.debug("Could not finalize interaction %s: %s", self.interaction.id, exc)


async def _delete_later(message: Any, delay: float) -> None:
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await message.delete()


async def auto_ack(replier: InteractionReplier, delay: float = ACK_AFTER_SECONDS) -> None:
    """Defer the interaction if the handler is still working close to Discord's 3-second deadline."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    try:
        # Shielded: once the defer is on the wire, the handler finishing must not cut it off half-way.
        await asyncio.shield(replier.ack())
    except asyncio.CancelledError:
        return
    except BotAPIError as exc:
        logger.debug("Auto-ack failed for interaction %s: %s", replier.interaction.id, exc)


__all__ = ["ACK_AFTER_SECONDS", "InteractionReplier", "auto_ack"]
