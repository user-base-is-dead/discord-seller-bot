from __future__ import annotations

from app.bot.callbacks import LangCB, NavCB
from app.bot.framework.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn, url_btn
from app.core.config import get_settings
from app.locales.i18n import supported_locales, t

_LOCALE_LABEL = {"en": "🇬🇧 English"}

# Green for the way in (the shop), red for the admin row, blue for everything else — so the eye lands
# on "buy" first and nothing else competes with it.
_STYLE = {
    "categories": SUCCESS,
    "admin_panel": DANGER,
    "profile": PRIMARY,  # 👛 Wallet — the nav token predates the rename
    "orders": PRIMARY,
    "warranty": PRIMARY,
    "support": PRIMARY,
}


def main_inline_keyboard(locale: str, *, is_admin: bool) -> InlineKeyboardMarkup:
    """Main menu rendered as buttons attached to the message itself, so they're visible in the
    chat without the user needing to open a separate reply-keyboard panel. The admin row is
    appended only when `is_admin` is true at render time — never a static keyboard baked in once
    and reused.

    One button per row, each with a line above it saying what it opens and a line between it and
    the next: the renderer gives every row the same width, so the menu comes out as one even stack
    of full-width buttons."""

    def _btn(key: str, target: str) -> InlineKeyboardButton:
        return btn(t(key, locale), NavCB(target=target).pack(), _STYLE[target], description=t(f"{key}_hint", locale))

    rows = [
        [_btn("menu.products", "categories")],
        [_btn("menu.wallet", "profile")],
        [_btn("menu.orders", "orders")],
        [_btn("menu.warranty", "warranty")],
        [_btn("menu.support", "support")],
    ]
    # Url buttons, not callback ones: they open the channel/group directly instead of costing the
    # user a round trip through the bot. Each is skipped when its link is not configured, so a
    # button is never dead — and both disappear when neither is.
    settings = get_settings()
    for key, url in (
        ("menu.channel", settings.community_channel_url.strip()),
        ("menu.community", settings.community_group_url.strip()),
    ):
        if url:
            rows.append([url_btn(t(key, locale), url, PRIMARY, description=t(f"{key}_hint", locale))])
    if is_admin:
        rows.append([_btn("menu.admin_panel", "admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# There is no reply-keyboard panel any more — see `app.bot.panel`. The `MenuButton` filter and its
# handlers stay: a client that still shows the old panel keeps working until the removal reaches it.


def language_inline_keyboard(locale: str = "en") -> InlineKeyboardMarkup:
    rows = [
        [btn(_LOCALE_LABEL[loc], LangCB(locale=loc).pack(), PRIMARY)] for loc in supported_locales()
    ]
    return with_nav(rows, locale, back_target="home", home=False)
