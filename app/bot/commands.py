"""The bot's slash commands.

Telegram showed each chat its own command menu (`setMyCommands` per scope). Discord registers one
global list, so every command is here once and the handlers decide who may run it — exactly as they
always did: a command is never authorised by being hidden.

Admin-only commands carry `admin=True`, which hides them from ordinary server members inside the
server (they need Manage Server to see them there). In DMs Discord shows every command to everybody;
a non-admin who runs one gets "not authorized" from the admin guard and an audit-log entry.

Staff commands (/close, /done, /refund, /reject, /cancel) are deliberately NOT admin-only: anyone who
can see the support or orders channel is staff and runs them inside a ticket or order thread.
"""

from __future__ import annotations

from app.bot.framework.client import CommandSpec

USER_COMMANDS = [
    CommandSpec("start", "Open the PowerX menu"),
    CommandSpec("products", "Open the shop"),
    CommandSpec("orders", "My orders"),
    CommandSpec("wallet", "My wallet"),
    CommandSpec("refunds", "Money owed back to me"),
    CommandSpec("warranty", "Warranty and claims"),
    CommandSpec("support", "Talk to our support team"),
    CommandSpec("language", "Change language"),
    CommandSpec("help", "How to use the bot"),
    CommandSpec("cancel", "Stop the form you're filling in"),
]

# Typed inside a ticket or order thread by whoever is working it.
STAFF_COMMANDS = [
    CommandSpec("close", "Staff: close this ticket or order thread"),
    CommandSpec("done", "Staff: warranty claim resolved — item replaced", takes_args=True, args_hint="Warranty ID"),
    CommandSpec(
        "refund",
        "Staff: settle a warranty claim with a refund",
        takes_args=True,
        args_hint="Warranty ID, then an optional reason",
    ),
    CommandSpec(
        "reject",
        "Staff: turn a warranty claim down",
        takes_args=True,
        args_hint="Warranty ID, then an optional reason",
    ),
]

ADMIN_COMMANDS = [
    CommandSpec("admin", "Admin panel", admin=True),
    CommandSpec(
        "adjust_balance",
        "Adjust a user's wallet balance",
        takes_args=True,
        args_hint="Discord user ID, signed amount (+10.00 / -5), reason",
        admin=True,
    ),
    CommandSpec("pending_orders", "Orders awaiting fulfilment", admin=True),
    CommandSpec("refund_wallets", "Refunds waiting to be settled", admin=True),
    CommandSpec("wallet_balances", "Who is holding store credit", admin=True),
    CommandSpec("open_tickets", "Support queue", admin=True),
    CommandSpec("dashboard", "Sales and user stats", admin=True),
    CommandSpec("broadcast_status", "How far a broadcast has got", takes_args=True, args_hint="Broadcast ID", admin=True),
]

ALL_COMMANDS = [*USER_COMMANDS, *STAFF_COMMANDS, *ADMIN_COMMANDS]
