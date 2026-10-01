# PowerX Digital-Product Store Bot — Discord

The Discord port of the PowerX Telegram store bot (`tg-kiro-sell-bot`). Same store, same rules, same
database schema: catalog and stock, credential-level holds, wallet + USDT (BEP-20) checkout with the
on-chain payment checker, orders with per-order staff threads, manual fulfilment, declines into the
Refund/Frozen wallets, warranty claims (`/done` `/refund` `/reject`), support tickets relayed between
DMs and staff threads, bans, the admin panel, CSV import/export, announcements and broadcasts.

Python 3.11+, discord.py 2.7, PostgreSQL (or SQLite locally) + SQLAlchemy 2 async, Alembic, optional
Redis, APScheduler.

## How the Telegram bot maps onto Discord

| Telegram | Discord |
|---|---|
| Private chat with the bot | The buyer's **DMs** with the bot — the whole store lives there |
| `t.me/<bot>` link | `/storefront` posts an **Open Store** card in a server channel; its button (or `/start` anywhere in the server) opens the store in DMs |
| Inline keyboard | Buttons on a card. Long lists fold into a dropdown; page indicators are disabled buttons |
| Copy-text button | Replies with the exact value in a message only the buyer sees, ready to copy |
| Bot commands menu | Slash commands (admin ones are hidden from server members without *Manage Server*) |
| Support / orders **forum groups** with a topic per ticket / order | Two private **channels** (forum channels recommended) with a **thread** per ticket / order |
| Staff = members of those groups | Staff = whoever can see those channels (e.g. a Staff role) |
| Closing a topic | Archiving and locking the thread |

The handlers are the Telegram bot's, unchanged in behaviour. `app/bot/framework/` provides the aiogram-style
API they were written against (routers, filters, `CallbackData`, FSM, `message.answer`,
`query.answer`, …) on top of discord.py, and converts the bot's HTML messages to Discord markdown.

## Discord setup

1. **Create the application** — <https://discord.com/developers/applications> → *New Application* →
   *Bot* → *Reset Token* and copy it (`DISCORD_TOKEN`).
2. **Enable the Message Content intent** — *Bot* → *Privileged Gateway Intents* → *Message Content
   Intent*. Without it the bot cannot read what staff type in ticket and order threads.
3. **Invite the bot** — *OAuth2* → *URL Generator*, scopes `bot` and `applications.commands`, or use
   this URL with your application id (permissions `326417615872` = View Channels, Send Messages, Send
   Messages in Threads, Create Public Threads, Manage Threads, Attach Files, Read Message History):

   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=326417615872
   ```
4. **Create the staff channels** — a private `#support` and a private `#orders` channel (forum
   channels work best; text channels work too — the bot opens a public thread per ticket there, visible
   only to people who can see the channel). Give your Staff role and the bot access. Optionally a
   `#bot-log` text channel for error tracebacks.
5. **Copy the ids** — *User Settings* → *Advanced* → *Developer Mode*, then right-click a channel or a
   user → *Copy ID*.
6. **Post the storefront** — run `/storefront` in the channel where buyers should find the store.

Buyers must share a server with the bot and allow DMs from its members (*Server menu → Privacy
Settings*). The storefront card says so.

## Configuration

Everything is read from `.env` next to this file. It is git-ignored and must never be committed.

```ini
# required
DISCORD_TOKEN=...
DATABASE_URL=sqlite+aiosqlite:///./local.db        # or postgresql+asyncpg://user:pass@host/db
ENCRYPTION_KEY=...                                 # python scripts/gen_encryption_key.py
WALLET_ADDRESS=0x...                               # the BEP-20 address buyers pay USDT into

# usually
ADMIN_IDS=123456789012345678,234567890123456789    # Discord user ids, comma-separated
SUPPORT_CHANNEL_ID=...
ORDERS_CHANNEL_ID=...
LOG_CHANNEL_ID=
REDIS_URL=redis://localhost:6379/0                  # blank = in-memory forms and flood limits

# optional
BSC_RPC_URL=https://bsc-dataseed1.bnbchain.org/    # use a keyed endpoint in production
BSC_RPC_LOG_SPAN=500
COMMUNITY_CHANNEL_URL=                              # links on the home screen; blank hides them
COMMUNITY_GROUP_URL=
DEFAULT_CURRENCY=USD
ACCENT_COLOR=#5865F2                                # card side stripe; blank for none
SYNC_COMMANDS=true                                  # push slash commands at startup
CRYPTO_WEBHOOK_SECRET=
ENVIRONMENT=development
LOG_LEVEL=INFO
```

The full list, with defaults, is in `app/core/config.py`.

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements-dev.txt

python scripts/gen_encryption_key.py   # paste into .env as ENCRYPTION_KEY
python -m scripts.bootstrap_db         # creates the schema (or migrates an existing one)
python -m app.main
```

`python -m scripts.doctor` reports what a database is missing relative to the code (read-only).
Slash commands are synced at startup and can take a few minutes to appear in the Discord client.

## Deploying

**Docker Compose** (bot + Postgres + Redis): create `.env` with the bot-specific values, then

```bash
docker compose up -d --build
docker compose logs -f bot
```

**systemd**: clone to `/opt/discord-store-bot`, create the venv and `.env`, run
`python -m scripts.bootstrap_db`, then install `deploy/discord-store-bot.service`.

## Tests

```bash
pytest
```

`tests/unit` covers the framework and the discord.py bridge against fake Discord objects;
`tests/integration` drives complete flows — shopping, USDT checkout, support relay, staff fulfilment
and declines in order threads, warranty claims, bans, the admin wizards, broadcasts and the scheduled
jobs — through the real dispatcher and a real SQLite database, rendering every screen with the real
Components V2 renderer so a screen Discord would reject fails the test.

## Differences worth knowing

- Discord has no reply keyboard, so the Telegram bottom panel has no equivalent (it was already retired).
- Admin slash commands are visible to everyone in DMs (Discord only hides commands inside servers); a
  non-admin who runs one is refused and the attempt is audit-logged, as on Telegram.
- Broadcasts send about two DMs per second to stay clear of Discord's anti-spam limits.
- Relayed and broadcast files over 10 MB are linked instead of re-uploaded.
- `/language` works here. On Telegram it was advertised but its handler was never registered.
