from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Fails fast on missing/invalid env vars — never falls back to a silent default for secrets."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="DISCORD_TOKEN")
    # Discord user ids (enable Developer Mode → right-click a user → Copy User ID), comma-separated.
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    database_url: str = Field(alias="DATABASE_URL")
    # Optional. Set it in production so FSM forms and flood limits survive a restart and are shared
    # between processes; blank keeps both in memory, which is fine for a single local process.
    redis_url: str = Field(default="", alias="REDIS_URL")

    # The staff side lives in the store's Discord server. Each of these is a channel id — a FORUM
    # channel (recommended: one post per ticket/order, like Telegram forum topics) or a plain text
    # channel (one public thread per ticket/order). Whoever can see the channel is staff, exactly as
    # membership of the Telegram staff group was: keep these channels private to the team.
    support_channel_id: int | None = Field(default=None, alias="SUPPORT_CHANNEL_ID")
    # A SEPARATE channel where every order gets its own thread, the way every ticket gets one in the
    # support channel. Deliberately not the support channel: those threads are conversations with a
    # buyer, and interleaving a running order log with them would bury the messages staff must
    # actually answer.
    #
    # A delivered order's thread closes itself. A cancelled or refunded one stays open and becomes a
    # dispute the buyer is connected to — their DMs relay into it and staff replies come back out —
    # until someone in the orders channel runs /close in the thread.
    #
    # Unset means no order threads are opened at all: the bot works exactly as before and a refund
    # falls back to an ordinary ticket in the support channel, so this is safe to leave blank.
    orders_channel_id: int | None = Field(default=None, alias="ORDERS_CHANNEL_ID")
    # A text channel for unhandled-error tracebacks. Optional.
    log_channel_id: int | None = Field(default=None, alias="LOG_CHANNEL_ID")

    # Official PowerX links shown on the welcome screen and as buttons under the main menu. Each one
    # is independent: blank hides that link and its button entirely, so a deployment never
    # advertises a dead one.
    community_channel_url: str = Field(default="", alias="COMMUNITY_CHANNEL_URL")
    community_group_url: str = Field(default="", alias="COMMUNITY_GROUP_URL")

    default_currency: str = Field(default="USD", alias="DEFAULT_CURRENCY")
    default_locale: str = Field(default="en", alias="DEFAULT_LOCALE")

    # The side stripe of every screen (a hex colour). Blank for Discord's neutral card.
    accent_colour_raw: str = Field(default="#5865F2", alias="ACCENT_COLOR")
    # How wide every row of buttons is drawn, in desktop pixels: a lone button fills it, buttons
    # sharing a row split it. Wider looks fuller on a computer, but past ~290 a phone may cut the
    # padded labels short — check on a phone after raising it.
    button_width: int = Field(default=280, ge=120, le=480, alias="BUTTON_WIDTH")
    # Push the slash command list to Discord at startup. Discord rate-limits syncs, so a deployment
    # that restarts in a loop can switch it off once the commands are in place.
    sync_commands: bool = Field(default=True, alias="SYNC_COMMANDS")

    encryption_key: str = Field(alias="ENCRYPTION_KEY")
    crypto_webhook_secret: str = Field(default="dev_secret", alias="CRYPTO_WEBHOOK_SECRET")
    wallet_address: str = Field(alias="WALLET_ADDRESS")
    # A BNB Chain JSON-RPC endpoint. The default public dataseed answers `eth_blockNumber` but
    # rate-limits `eth_getLogs`, so a keyed endpoint (NodeReal, Ankr, QuickNode) belongs here in
    # production — payment detection reads logs on every tick.
    bsc_rpc_url: str = Field(default="https://bsc-dataseed1.bnbchain.org/", alias="BSC_RPC_URL")
    # How many blocks one `eth_getLogs` request may span. Providers enforce their own cap and
    # reject anything wider outright, so this is tunable per endpoint rather than hard-coded.
    bsc_rpc_log_span: int = Field(default=500, alias="BSC_RPC_LOG_SPAN")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("support_channel_id", "orders_channel_id", "log_channel_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_ids_raw.split(",") if x.strip()]

    @property
    def accent_colour(self) -> int | None:
        raw = self.accent_colour_raw.strip().lstrip("#")
        if not raw:
            return None
        try:
            return int(raw, 16)
        except ValueError:
            return None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
