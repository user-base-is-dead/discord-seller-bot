"""The first message in every staff thread: how to work it, and the commands that work there.

New support staff land in channels full of threads the bot opened, and nothing in them said how to
answer a buyer, how to close a ticket, or what /done means — the commands were only discoverable by
reading the code. So every thread the bot opens starts with a guide, tailored to what the thread is:
a plain support ticket, a warranty claim, or an order's log.

Each guide lists only the commands that belong to its channel. A support thread shows the support
commands and an order thread the order ones — the full admin command list used to be appended to
every guide, so a ticket thread advertised /pending_orders and /broadcast_status, and the commands
that mattered there were lost in a list of ones that did not.

Kept in one place so the guides cannot drift apart, and so a new command is documented where staff
will actually see it.
"""

from __future__ import annotations

from app.services.warranty_service import CLAIM_GRACE

_REPLY = (
    "<h3>💬 Reply to the buyer</h3>"
    "Just type in this thread — your text reaches the buyer's DMs as “🎧 PowerX Support”, formatting "
    "included, and photos, videos, files and voice messages are passed on too.\n"
    "<small>Anything that starts with / stays in this thread and is never sent to the buyer.</small>"
)

# Admin commands that belong to each channel. The rest (/admin, /dashboard, /adjust_balance,
# /wallet_balances, /broadcast_status) are store-wide tools and live in the admin panel.
_SUPPORT_ADMIN = (("/open_tickets", "every open support ticket, with a button to close each one"),)
_ORDER_ADMIN = (
    ("/pending_orders", "every order waiting to be fulfilled"),
    ("/refund_wallets", "refunds waiting to be settled, and frozen money to release"),
)


def _commands(title: str, commands: tuple[tuple[str, str], ...], admin: tuple[tuple[str, str], ...]) -> str:
    lines = [f"<code>{name}</code> — {what}" for name, what in commands]
    lines += [f"<code>{name}</code> — {what} · <i>admins only</i>" for name, what in admin]
    return f"<h3>{title}</h3>" + "\n".join(lines)


def _anyone(what: str) -> str:
    # Channel access is the staff list (app/bot/filters/staff.py), so a new team member needs to be
    # told they can act, not just how.
    return f"<small>👥 Everyone who can see this channel can {what} — no admin ID needed.</small>"


def ticket_guide() -> str:
    """For an ordinary support ticket's thread in the support channel."""
    return (
        "<h2>📋 Staff guide · Support ticket</h2>"
        "<small>The buyer's details are on the card below, then their message.</small>"
        "<hr>"
        f"{_REPLY}"
        "<hr>"
        + _commands(
            "🎧 Support commands",
            (("/close", "close the ticket. The buyer is told, and this thread is archived and locked."),),
            _SUPPORT_ADMIN,
        )
        + "<hr>"
        + _anyone("reply and close tickets")
    )


def warranty_claim_guide() -> str:
    """For a warranty claim's thread: the claim has three endings of its own, on top of chatting."""
    hours = int(CLAIM_GRACE.total_seconds() // 3600)
    return (
        "<h2>📋 Staff guide · Warranty claim</h2>"
        "<small>The warranty ID is in the claim message below.</small>"
        "<hr>"
        f"{_REPLY}"
        "<hr>"
        "<h3>🛡️ Settle the claim</h3>"
        "<code>/done ID</code> — item replaced: the buyer keeps the warranty time that was left, and "
        "this thread closes.\n"
        "<code>/refund ID reason</code> — pay them back instead: the money goes to their Refund "
        "Wallet, the warranty ends, and this thread stays open until the money is settled.\n"
        "<code>/reject ID reason</code> — turn the claim down: the original warranty carries on, and "
        "this thread closes.\n"
        f"<small>⏱️ If nobody answers within {hours} hours, the claim closes by itself.</small>"
        "<hr>"
        + _commands(
            "🎧 Support commands",
            (("/close", "close the thread once a refunded claim's money is settled."),),
            _SUPPORT_ADMIN,
        )
        + "<hr>"
        + _anyone("reply, settle claims and close threads")
    )


def order_guide() -> str:
    """For an order's thread in the orders channel — a log for staff, never a chat with the buyer."""
    return (
        "<h2>📋 Staff guide · Order thread</h2>"
        "<small>Staff only — nothing typed here reaches the buyer.</small>"
        "This thread is the order's log: the card below always shows where the order stands, and "
        "every step is posted under it."
        "<hr>"
        "<h3>🛠️ Manual orders</h3>"
        "Use the buttons on the “awaiting fulfilment” message:\n"
        "<b>✅ Fulfill now</b> — then send the delivery content as your next message here. The buyer "
        "gets it exactly as you format it.\n"
        "<b>🚫 Decline</b> — then send the reason and pick where their money goes: ↩️ Refund Wallet "
        "(settled with them later) or 🧊 Frozen Wallet (on hold until you unfreeze it). The buyer "
        "reads the reason word for word."
        "<hr>"
        + _commands(
            "📦 Order commands",
            (
                ("/cancel", "stop a fulfil or decline you started."),
                (
                    "/close",
                    "close this thread. Delivered orders close by themselves; a declined order's "
                    "thread stays open while its refund is parked — close it once you're done.",
                ),
            ),
            _ORDER_ADMIN,
        )
        + "<hr>"
        + _anyone("fulfil and decline orders here")
        + "<small>💸 Paying out, moving or unfreezing the money afterwards is for admins.</small>"
    )
