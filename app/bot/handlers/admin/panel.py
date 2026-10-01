from __future__ import annotations

from app.bot.callbacks import (
    AdminCategoryCB,
    AdminMiscCB,
    AdminOrderCB,
    AdminProductCB,
    NavCB,
)
from app.bot.filters.is_admin import IsAdmin
from app.bot.filters.menu_button import MenuButton
from app.bot.framework import F, Router
from app.bot.framework.filters import Command
from app.bot.framework.types import CallbackQuery, InlineKeyboardMarkup, Message
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import PRIMARY, SUCCESS, btn
from app.database.models.user import User

router = Router(name="admin.panel")
router.message.filter(IsAdmin())  # every handler in this router independently requires admin


# One copy, used by both the /admin command and the nav callback. It previously existed as two
# verbatim paste-ups that could drift apart on any edit. What each button opens is written above
# the button itself, so the text only carries what has no button. Kept to one block of short lines:
# the panel uses its whole component budget, and nothing here may be wider than the buttons.
_PANEL_TEXT = (
    "🛡️ <b>Admin Panel</b>\n"
    "<i>Everything that runs the store.</i>\n\n"
    "<b>Quick commands</b>\n"
    "<code>/adjust_balance</code> — credit/debit a wallet\n"
    "<code>/open_tickets</code> — the support queue"
)


def _panel_keyboard(locale: str) -> InlineKeyboardMarkup:
    # No Payments button: it opened the *manual* top-up review queue, and top-ups are automatic
    # crypto now. app/bot/handlers/admin/payments.py stays registered so its approve/reject
    # callbacks keep working if a manual provider is ever put back in front of users.
    #
    # One button per row, its description above it: every button in the panel the same full width.
    return with_nav(
        [
            [btn("📊 Dashboard", AdminMiscCB(action="dashboard").pack(), PRIMARY, description="Sales, revenue and user activity")],
            [btn("📦 Products", AdminProductCB(action="list").pack(), PRIMARY, description="Inventory, prices, stock and listings")],
            [btn("📁 Categories", AdminCategoryCB(action="list").pack(), PRIMARY, description="The folders buyers browse")],
            [btn("👥 Users", AdminMiscCB(action="users").pack(), PRIMARY, description="Accounts, balances and bans")],
            [btn("🛒 Orders", AdminOrderCB(action="list").pack(), PRIMARY, description="Fulfilment queue, search and history")],
            # Green: the only two screens about money — what the store owes people, and what buyers
            # can still spend here.
            [btn("💸 Refund Wallets", AdminMiscCB(action="refunds").pack(), SUCCESS, description="Refunds to settle, frozen money")],
            [btn("💳 Wallet Balances", AdminMiscCB(action="balances").pack(), SUCCESS, description="Accounts holding spendable credit")],
            [btn("📢 Broadcast", AdminMiscCB(action="broadcast").pack(), PRIMARY, description="Send a message to every user")],
            [btn("📝 Logs", AdminMiscCB(action="logs").pack(), PRIMARY, description="System logs and audit events")],
        ],
        locale,
        back_target="home",
        home=False,
    )


@router.message(Command("admin"))
@router.message(MenuButton("menu.admin_panel"))
async def show_admin_panel(message: Message, user: User) -> None:
    await message.answer(_PANEL_TEXT, reply_markup=_panel_keyboard(user.locale))


@router.callback_query(NavCB.filter(F.target == "admin_panel"), IsAdmin())
async def nav_admin_panel(query: CallbackQuery, callback_data: NavCB, user: User) -> None:
    if not query.message:
        return
    await query.message.edit_text(_PANEL_TEXT, reply_markup=_panel_keyboard(user.locale))
    await query.answer()
