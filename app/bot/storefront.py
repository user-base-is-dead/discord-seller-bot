"""The store's front door inside a Discord server.

On Telegram a buyer reached the bot through its t.me link and the whole store lived in that private
chat. On Discord the store still lives in private — the buyer's DMs with the bot — but people meet
it in a server, so an admin posts a storefront card in a public channel with `/storefront`. Its
button (and `/start` used anywhere in the server) opens the store in the buyer's DMs.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.filters.is_admin import is_admin_user
from app.bot.framework.bot import Bot
from app.bot.framework.client import OPEN_STORE, StoreClient
from app.bot.framework.types import InlineKeyboardMarkup
from app.bot.keyboards.styles import SUCCESS, btn
from app.database.repositories.audit_repo import AuditRepo
from app.locales.i18n import t

logger = logging.getLogger(__name__)


def storefront_view(bot: Bot, locale: str = "en") -> discord.ui.LayoutView:
    """The storefront card, drawn like every other screen — the store's logo beside its name."""
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn(t("storefront.button", locale), OPEN_STORE, SUCCESS)]])
    return bot.render(t("storefront.body", locale), markup)


def register_storefront(client: StoreClient, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    @app_commands.command(name="storefront", description="Admin: post the Open Store button in this channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def storefront(interaction: discord.Interaction) -> None:
        async with sessionmaker() as session:
            allowed = await is_admin_user(session, interaction.user.id)
            if not allowed:
                await AuditRepo(session).log(
                    actor_discord_id=interaction.user.id,
                    action="security.unauthorized_admin",
                    target_type="command",
                    target_id="/storefront",
                    metadata={"attempted": "/storefront", "surface": "command"},
                )
                await session.commit()
        if not allowed:
            await interaction.response.send_message(t("common.unauthorized", "en"), ephemeral=True)
            return

        channel = interaction.channel
        try:
            await channel.send(view=storefront_view(client.bot), allowed_mentions=discord.AllowedMentions.none())  # type: ignore[union-attr]
        except (discord.HTTPException, AttributeError) as exc:
            logger.warning("Couldn't post the storefront in channel %s (%s)", getattr(channel, "id", "?"), exc)
            await interaction.response.send_message(
                "⚠️ I can't post in this channel. Give me View Channel and Send Messages here, then try again.",
                ephemeral=True,
            )
            return

        async with sessionmaker() as session:
            await AuditRepo(session).log(
                actor_discord_id=interaction.user.id,
                action="storefront.post",
                target_type="channel",
                target_id=str(getattr(channel, "id", "")),
            )
            await session.commit()
        await interaction.response.send_message("✅ Storefront posted.", ephemeral=True)

    client.tree.add_command(storefront)
