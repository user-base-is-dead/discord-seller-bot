from __future__ import annotations

from typing import Final

from app.bot.framework.types import CopyTextButton, InlineKeyboardButton

# Button colours, rendered as Discord button styles:
#   PRIMARY → blurple, SUCCESS → green, DANGER → red, NEUTRAL → grey (secondary).
# They live here as constants so every keyboard picks one of them rather than passing a raw string.
PRIMARY: Final = "primary"  # blurple — navigation and neutral primary actions
SUCCESS: Final = "success"  # green   — money-in, confirm, approve, create
DANGER: Final = "danger"  # red     — cancel, delete, reject, leave a screen

# Grey. Used for inert filler (page indicators) and long scrollable lists, where a wall of solid
# colour reads as noise.
NEUTRAL: Final = None


def btn(
    text: str, callback_data: str, style: str | None = NEUTRAL, *, description: str | None = None
) -> InlineKeyboardButton:
    """Every styled button in the bot goes through here, so the `style` argument can never drift
    into a value the renderer does not know. A `description` is written just above the button when
    it is alone on its row."""
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style, description=description)


def copy_btn(text: str, value: str, style: str | None = PRIMARY) -> InlineKeyboardButton:
    """A button that hands the buyer `value` to copy, whole.

    Discord has no clipboard buttons, so pressing it answers with the value alone in a message only
    they can see — long-press (mobile) or select (desktop) copies exactly that and nothing else.
    Worth a button rather than leaving it to selecting text: the copy target is a payment amount
    whose last decimals are what identify the payer, and a partial selection or a hand-typed digit
    is a payment nobody can attribute. Pressing it never leaves the payment screen.
    """
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value), style=style)


def url_btn(
    text: str, url: str, style: str | None = PRIMARY, *, description: str | None = None
) -> InlineKeyboardButton:
    """A link button. Discord draws every link button grey with an ↗ marker, whatever `style` says."""
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, url=url, style=style, description=description)
