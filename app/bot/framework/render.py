"""Turn (text, InlineKeyboardMarkup) into a Discord message.

Screens are Components V2 messages: one Container (the card) holding the text and the button rows.
V2 is what makes a Telegram keyboard fit at all — classic messages allow five rows of buttons, V2
allows forty components in total, so a list of twelve products keeps one button per product exactly
as on Telegram.

The card itself:
  * its text is cut into blocks at every divider (`<hr>` in the template), each block a TextDisplay
    with a hairline Separator between them;
  * a thumbnail, when the template names one, sits beside the first block (a Section);
  * a hairline separates the text from the buttons whenever the component budget has room for it;
  * every button row is brought to the same width, so a keyboard reads as one even block (see
    "button widths" below), and a Telegram ruled-line filler button becomes a real divider;
  * the side stripe is the brand colour, unless the card opens on a status emoji: green for done
    (✅ 🟢), red for refused (🚫 ⛔), amber for waiting or a warning (⏳ ⚠️), grey for closed (🔒).

When a screen still exceeds forty components (a long admin list), the renderer degrades in the least
surprising order:
  1. a run of single-button rows (a list: products, orders, tickets) becomes a dropdown whose options
     are those same buttons — selecting one is dispatched exactly like pressing it;
  2. small rows are packed side by side (at most five buttons per row);
  3. purely decorative disabled rows (separators) are dropped.

Button semantics:
  * `callback_data` → custom_id; "noop" (page indicators, separators) renders disabled;
  * `url` → link button; `copy_text` → a button that answers with the value on its own so it can be
    copied (Discord has no clipboard buttons);
  * duplicate custom_ids — legal on Telegram, rejected by Discord — get a `#n` suffix that the
    dispatcher strips again.

Every view is `stop()`ped before it is sent, so discord.py never stores it: all interactions are
routed by custom_id through the dispatcher instead, which is what lets a button keep working after a
restart, like a Telegram callback button does.
"""

from __future__ import annotations

import itertools
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import discord

from app.bot.framework.markdown import SEPARATOR, escape_markdown
from app.bot.framework.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

MAX_COMPONENTS = 40
MAX_ROW = 5
MAX_BUTTON_LABEL = 80
MAX_OPTION_LABEL = 100
MAX_OPTIONS = 25
MAX_CUSTOM_ID = 100

NOOP = "noop"
COPY_PREFIX = "copy:"
SELECT_PREFIX = "sel:"
DEDUPE_SEPARATOR = "#"

_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    None: discord.ButtonStyle.secondary,
}

# Card stripes for the status a card opens on. Discord's own palette, so they sit well next to the
# button colours.
SUCCESS_COLOUR = 0x57F287
DANGER_COLOUR = 0xED4245
WARNING_COLOUR = 0xF0B232
FROZEN_COLOUR = 0x5DADE2
MUTED_COLOUR = 0x80848E

_TONES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("✅", "🟢", "🎉", "📬"), SUCCESS_COLOUR),
    (("🚫", "⛔", "❌", "🔴", "🛑"), DANGER_COLOUR),
    (("⚠", "🟠", "🟡", "⏳", "⌛", "🐢", "🛠"), WARNING_COLOUR),
    (("🧊",), FROZEN_COLOUR),
    (("🔒", "✖", "⚫"), MUTED_COLOUR),
)
# Markdown in front of a card's first emoji: a heading or subtext marker, a quote, bold, a joiner.
_LEADING_SYNTAX = re.compile(r"^(?:#{1,3}\s|-#\s|[\s\u200b>*_~|])+")


def tone_colour(markdown: str) -> int | None:
    """The stripe a card's opening emoji asks for, or None to keep the brand colour."""
    head = _LEADING_SYNTAX.sub("", markdown.lstrip(SEPARATOR + "\n"))
    for emojis, colour in _TONES:
        if head.startswith(emojis):
            return colour
    return None


def split_blocks(text: str) -> list[str]:
    """The card's text cut at its dividers, without empty blocks."""
    blocks = [block.strip("\n") for block in text.split(SEPARATOR)]
    blocks = [block for block in blocks if block.strip() and block.strip() != "\u200b"]
    return blocks or ["\u200b"]

# Values too long to ride in a custom_id, keyed by a short token. In-memory on purpose: a copy button
# only has to outlive the screen it is on, and a restart re-renders every live screen anyway.
_COPY_VALUES: dict[str, str] = {}
_COPY_LIMIT = 2048


def copy_custom_id(value: str) -> str:
    candidate = COPY_PREFIX + value
    if len(candidate) <= MAX_CUSTOM_ID:
        return candidate
    token = secrets.token_hex(6)
    if len(_COPY_VALUES) >= _COPY_LIMIT:
        _COPY_VALUES.pop(next(iter(_COPY_VALUES)))
    _COPY_VALUES[token] = value
    return f"{COPY_PREFIX}@{token}"


def copy_value(custom_id: str) -> str | None:
    value = custom_id[len(COPY_PREFIX) :]
    if value.startswith("@"):
        return _COPY_VALUES.get(value[1:])
    return value


_DEDUPE_RE = re.compile(re.escape(DEDUPE_SEPARATOR) + r"\d+$")


def strip_dedupe(custom_id: str) -> str:
    return _DEDUPE_RE.sub("", custom_id)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return "·"
    return text if len(text) <= limit else text[: limit - 1] + "…"


_EXIT_MARKERS = ("⬅️", "🔙", "🏠", "✖️", "❌")


def _is_exit(button: InlineKeyboardButton) -> bool:
    """Back / Home / Cancel: the way off a screen. Never folded into a dropdown with the list above it."""
    data = button.callback_data or ""
    return data.startswith("nav:") or button.text.strip().startswith(_EXIT_MARKERS)


def _is_list_row(row: list[InlineKeyboardButton]) -> bool:
    """A row that is one real, tappable button — the shape of an item in a list."""
    return (
        len(row) == 1
        and row[0].callback_data is not None
        and row[0].callback_data != NOOP
        and row[0].url is None
        and row[0].copy_text is None
        and not _is_exit(row[0])
    )


def _is_decorative(row: list[InlineKeyboardButton]) -> bool:
    return all(b.callback_data == NOOP and b.url is None and b.copy_text is None for b in row)


@dataclass
class _Row:
    # "buttons": a row of buttons · "entry": one button with its description above it ·
    # "select": a dropdown · "rule": a divider drawn in place of a ruled-line filler button ·
    # "divider": the line drawn between two entries
    kind: str
    buttons: list[InlineKeyboardButton]

    @property
    def cost(self) -> int:
        if self.kind in ("rule", "divider"):
            return 1  # a separator
        if self.kind == "entry":
            return 3  # its description, the action row and the button
        return 1 + (len(self.buttons) if self.kind == "buttons" else 1)


def _is_entry(row: list[InlineKeyboardButton]) -> bool:
    """A button alone on its row that carries a description: drawn with the description above it."""
    return len(row) == 1 and bool((row[0].description or "").strip())


_RULE_LABEL = re.compile(r"^[\s─━═—–\-_]+$")


def _is_rule(row: list[InlineKeyboardButton]) -> bool:
    """A Telegram-style divider: one inert button whose label is nothing but a ruled line. Discord
    has real dividers, so it is drawn as one instead of as a grey button with dashes on it."""
    return (
        len(row) == 1
        and _is_decorative(row)
        and bool(row[0].text.strip())
        and bool(_RULE_LABEL.match(row[0].text))
    )


def _normalize(markup: Any) -> list[list[InlineKeyboardButton]]:
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        buttons = [b for b in row if isinstance(b, InlineKeyboardButton) and _usable(b)]
        for start in range(0, len(buttons), MAX_ROW):
            chunk = buttons[start : start + MAX_ROW]
            if chunk:
                rows.append(chunk)
    return rows


def _usable(button: InlineKeyboardButton) -> bool:
    if button.url is not None:
        return bool(re.match(r"(https?|discord)://", button.url, re.I))
    return button.callback_data is not None or button.copy_text is not None


def _plan(rows: list[list[InlineKeyboardButton]], budget: int) -> list[_Row]:
    plan: list[_Row] = []
    for row in rows:
        kind = "rule" if _is_rule(row) else "entry" if _is_entry(row) else "buttons"
        if kind == "entry" and plan and plan[-1].kind == "entry":
            plan.append(_Row("divider", []))
        plan.append(_Row(kind, row))

    def total() -> int:
        return sum(r.cost for r in plan)

    # 0. Menu entries give way first: the lines between them, then their descriptions.
    if total() > budget:
        plan = [r for r in plan if r.kind != "divider"]
    if total() > budget:
        plan = [_Row("buttons", r.buttons) if r.kind == "entry" else r for r in plan]

    # 1. Lists become dropdowns, longest run first.
    while total() > budget:
        best: tuple[int, int] | None = None
        start = None
        for index, item in enumerate([*plan, None]):
            is_list = item is not None and item.kind == "buttons" and _is_list_row(item.buttons)
            if is_list and start is None:
                start = index
            elif not is_list and start is not None:
                if index - start >= 2 and (best is None or index - start > best[1] - best[0]):
                    best = (start, index)
                start = None
        if best is None:
            break
        run = [r.buttons[0] for r in plan[best[0] : best[1]]]
        selects = [_Row("select", run[i : i + MAX_OPTIONS]) for i in range(0, len(run), MAX_OPTIONS)]
        plan[best[0] : best[1]] = selects

    # 2. Pack small neighbouring button rows together — each merge saves one action row.
    over = total() - budget
    if over > 0:
        packed: list[_Row] = []
        for item in plan:
            last = packed[-1] if packed else None
            if (
                over > 0
                and last is not None
                and last.kind == "buttons"
                and item.kind == "buttons"
                and len(last.buttons) + len(item.buttons) <= MAX_ROW
            ):
                last.buttons = [*last.buttons, *item.buttons]
                over -= 1
            else:
                packed.append(_Row(item.kind, list(item.buttons)))
        plan = packed

    # 3. Separators and other inert rows go.
    if total() > budget:
        plan = [r for r in plan if not (r.kind in ("buttons", "rule") and _is_decorative(r.buttons))]

    # 4. Last resort: keep the bottom rows (the way out) and as many of the top as fit.
    if total() > budget:
        logger.warning("Keyboard still over Discord's component budget after packing; trimming rows")
        tail = plan[-2:]
        head: list[_Row] = []
        used = sum(r.cost for r in tail)
        for item in plan[:-2]:
            if used + item.cost > budget:
                break
            head.append(item)
            used += item.cost
        plan = head + tail
    return plan


class _IdAllocator:
    def __init__(self) -> None:
        self.seen: dict[str, int] = {}
        self.noop = itertools.count()

    def take(self, custom_id: str) -> str:
        if custom_id == NOOP:
            return f"{NOOP}{DEDUPE_SEPARATOR}{next(self.noop)}"
        count = self.seen.get(custom_id, 0)
        self.seen[custom_id] = count + 1
        if count == 0:
            return custom_id
        suffixed = f"{custom_id}{DEDUPE_SEPARATOR}{count}"
        return suffixed if len(suffixed) <= MAX_CUSTOM_ID else custom_id[: MAX_CUSTOM_ID - 4] + suffixed[-4:]


def _button(button: InlineKeyboardButton, ids: _IdAllocator, *, label: str | None = None) -> discord.ui.Button:
    # Not `_truncate`d when a label is given: it is already sized, and stripping would eat padding.
    label = label if label else _truncate(button.text, MAX_BUTTON_LABEL)
    if button.url is not None:
        return discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=button.url)
    style = _STYLES.get(button.style, discord.ButtonStyle.secondary)
    if button.copy_text is not None:
        return discord.ui.Button(style=style, label=label, custom_id=ids.take(copy_custom_id(button.copy_text.text)))
    data = button.callback_data or NOOP
    if data == NOOP:
        return discord.ui.Button(
            style=discord.ButtonStyle.secondary, label=label, custom_id=ids.take(NOOP), disabled=True
        )
    return discord.ui.Button(style=style, label=label, custom_id=ids.take(data))


def _select(buttons: list[InlineKeyboardButton], index: int, placeholder: str) -> discord.ui.Select:
    values: set[str] = set()
    options = []
    for button in buttons:
        value = button.callback_data or NOOP
        base, n = value, 1
        while value in values:
            value = f"{base}{DEDUPE_SEPARATOR}{n}"
            n += 1
        values.add(value)
        options.append(discord.SelectOption(label=_truncate(button.text, MAX_OPTION_LABEL), value=value[:MAX_CUSTOM_ID]))
    return discord.ui.Select(custom_id=f"{SELECT_PREFIX}{index}", placeholder=placeholder, options=options)


# -- button widths ----------------------------------------------------------------------------
#
# Discord draws a button only as wide as its label, so a keyboard comes out ragged: "🛒 Shop" a
# stub, "🧾 My Orders" twice its size, every row a different length. Telegram stretched each row
# across the message; this is the nearest Discord allows. Every row of a card is brought to one
# width — a lone button fills it, buttons sharing a row split it equally — by padding each label on
# both sides with invisible spaces, so the text sits in the middle. A button's height is fixed by
# Discord; only its width can grow.
#
# The width is `button_width` (desktop pixels at 100% zoom) or, when one row of the card is
# naturally wider, that row's width, so every button of a card ends on the same line. The default
# fills the cards the store's own menus make, and stays narrow enough for a phone to show a padded
# label whole: on a phone a button already stretches across the card, and a label wider than that
# would be cut short.
#
# Label widths come from measured advance widths of Discord's 14px button font, scaled to the
# current gg sans. Estimates, so buttons match to within a few pixels rather than exactly.

DEFAULT_BUTTON_WIDTH = 280.0
_BUTTON_CHROME = 32.0  # horizontal padding inside a button
_BUTTON_GAP = 8.0  # space between two buttons in a row
_LINK_ICON = 20.0  # the ↗ Discord adds to a link button
_GROWTH_LIMIT = 1.3  # a card with one very long label doesn't pad every other button out to it
_SCALE = 0.84  # gg sans at 14px against the measured table below
_NBSP = "\u00a0"  # no-break space: never collapsed and never a place to wrap
# A space is narrower in gg sans than in the source table. Calibrated from two rendered buttons:
# the product label used 43 while Back used 70, and Back still rendered 6px shorter. That 27-space
# difference puts one no-break space at ≈2.73px on Discord desktop.
_SPACE = 3.25
_NBSP_WIDTH = _SPACE * _SCALE
_EN = "\u2002"  # en space, exactly half an em in any font: for widths no-break spaces can't reach
_EN_WIDTH = 7.0
_GUARD = "\u200b"  # zero-width, not whitespace: keeps the padding from being trimmed off the ends

_GLYPHS: dict[str, float] = {
    **dict(zip("0123456789", (8.67, 5, 7.47, 7.18, 8.25, 7.38, 7.9, 7.38, 7.95, 7.9), strict=True)),
    **dict(
        zip(
            "abcdefghijklmnopqrstuvwxyz",
            (6.98, 7.45, 6.53, 7.53, 6.98, 4.35, 7.05, 7.38, 3.4, 3.4, 6.73, 3.4, 11.42, 7.38, 7.38, 7.53,
             7.45, 5.02, 6.18, 4.72, 7.38, 6.58, 10.1, 6.6, 6.77, 6.32),
            strict=True,
        )
    ),
    **dict(
        zip(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            (9.63, 7.63, 8.85, 9.83, 7.08, 6.68, 9.75, 9.83, 3.8, 5.17, 8.52, 6.55, 12.6, 9.83, 10.43, 7.5,
             10.43, 7.98, 7.17, 8.4, 9.65, 9.47, 14.23, 8.9, 8.9, 8.33),
            strict=True,
        )
    ),
    " ": _SPACE, _NBSP: _SPACE, "/": 7.5, "*": 5.6, "-": 4.95, ",": 3.17, ".": 3.17, "_": 6.35, "?": 7.25,
    "!": 3.8, "#": 9.25, "@": 11.7, "$": 7.38, "%": 12.6, "^": 6.35, "&": 9.53, "=": 8.25, "+": 8.25,
    "<": 8.0, ">": 8.0, "\\": 7.5, "|": 3.72, '"': 5.72, "'": 3.17, ";": 3.3, ":": 3.3, "{": 6.67,
    "}": 6.67, "[": 5.72, "]": 5.72, "(": 5.47, ")": 5.47, "`": 6.35, "~": 5.72, "—": 14.0, "–": 7.0,
    "·": 3.4, "•": 5.0, "…": 10.5, "×": 7.4, "→": 12.0, "←": 12.0, "−": 8.25, "“": 5.72, "”": 5.72,
    "’": 3.17, _EN: _EN_WIDTH / _SCALE,
}
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x20E3, 0xFE0E, 0xFE0F}


def _glyph_width(char: str) -> float:
    """One character's width in table units (multiply by `_SCALE` for pixels)."""
    known = _GLYPHS.get(char)
    if known is not None:
        return known
    code = ord(char)
    if code in _ZERO_WIDTH or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F:
        return 0.0  # joiners, variation selectors, skin tones, keycap marks
    if code >= 0x1F000 or 0x2190 <= code <= 0x2BFF or 0x3000 <= code <= 0x33FF:
        return 18.0  # emoji and pictographs, drawn as images
    return 7.0


def _content_width(button: InlineKeyboardButton, label: str) -> float:
    """What a label takes up inside its button, in pixels."""
    width = sum(_glyph_width(char) for char in label) * _SCALE
    return width + (_LINK_ICON if button.url is not None else 0.0)


def _length(text: str) -> int:
    """Length as Discord counts it against the 80-character label limit (UTF-16 units)."""
    return len(text.encode("utf-16-le")) // 2


def _row_width(row: list[InlineKeyboardButton]) -> float:
    """A row's width with its buttons made equal and nothing padded: n × the widest label."""
    widest = max(_content_width(b, _truncate(b.text, MAX_BUTTON_LABEL)) for b in row)
    return len(row) * (widest + _BUTTON_CHROME) + _BUTTON_GAP * (len(row) - 1)


def card_width(rows: list[list[InlineKeyboardButton]], button_width: float = DEFAULT_BUTTON_WIDTH) -> float:
    """The one width every button row of a card is padded to."""
    widest = max((_row_width(row) for row in rows if row), default=0.0)
    return min(max(button_width, widest), button_width * _GROWTH_LIMIT)


def _padding(spare: float, room: int) -> tuple[int, int]:
    """(en spaces, no-break spaces) that fill `spare` pixels within `room` characters.

    No-break spaces alone when they fit — they can never become a place for a phone to wrap the
    label — with en spaces taking over the bulk only when the 80-character limit runs out first.
    """
    if spare < _NBSP_WIDTH / 2 or room <= 0:
        return 0, 0
    nbsps = round(spare / _NBSP_WIDTH)
    if nbsps <= room:
        return 0, nbsps
    for ens in range(1, room + 1):
        nbsps = max(0, round((spare - ens * _EN_WIDTH) / _NBSP_WIDTH))
        if ens + nbsps <= room:
            return ens, nbsps
    return room, 0


def _centred(label: str, spare: float) -> str:
    """`label` with `spare` pixels of padding split evenly around it."""
    room = MAX_BUTTON_LABEL - _length(label) - 2  # the two guards
    ens, nbsps = _padding(spare, room)
    if not ens and not nbsps:
        return label
    left = _EN * (ens // 2) + _NBSP * (nbsps // 2)
    right = _EN * (ens - ens // 2) + _NBSP * (nbsps - nbsps // 2)
    return f"{_GUARD}{left}{label}{right}{_GUARD}"


def _even_labels(row: list[InlineKeyboardButton], width: float) -> list[str]:
    """The row's labels, padded so its buttons share `width` equally.

    A label too long for its share widens the row rather than being cut, and the other buttons in
    the row grow with it — buttons sharing a row always come out the same size."""
    labels = [_truncate(button.text, MAX_BUTTON_LABEL) for button in row]
    widths = [_content_width(button, label) for button, label in zip(row, labels, strict=True)]
    share = max((width - _BUTTON_GAP * (len(row) - 1)) / len(row) - _BUTTON_CHROME, max(widths))
    return [_centred(label, share - used) for label, used in zip(labels, widths, strict=True)]


class _StaticLayoutView(discord.ui.LayoutView):
    """A layout discord.py never keeps in its view store.

    Every interaction is routed by custom_id through the dispatcher, so nothing may be claimed by a
    stored view first. `stop()` alone is not enough: a view built where no event loop is running has
    no "stopped" future, reports itself unfinished, and would be stored on send.
    """

    def is_finished(self) -> bool:
        return True


class _StaticView(discord.ui.View):
    """Classic-message counterpart of `_StaticLayoutView`."""

    def is_finished(self) -> bool:
        return True


def _hairline() -> discord.ui.Separator:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


# Each block costs a component and so does every hairline, out of the forty the buttons share. A
# template with more dividers than this keeps its first blocks and merges the rest into the last.
_MAX_BLOCKS = 8


def _text_items(text: str, thumbnail: str | None) -> tuple[list[discord.ui.Item[Any]], int]:
    """The card's text as components — blocks with hairlines between them, the first one beside the
    thumbnail — and how many components that costs."""
    blocks = split_blocks(text)
    if len(blocks) > _MAX_BLOCKS:
        blocks = [*blocks[: _MAX_BLOCKS - 1], "\n\n".join(blocks[_MAX_BLOCKS - 1 :])]
    items: list[discord.ui.Item[Any]] = []
    cost = 0
    for index, block in enumerate(blocks):
        if index:
            items.append(_hairline())
            cost += 1
        display = discord.ui.TextDisplay(block)
        if index == 0 and thumbnail:
            items.append(discord.ui.Section(display, accessory=discord.ui.Thumbnail(thumbnail)))
            cost += 3  # the section, its text and its thumbnail
        else:
            items.append(display)
            cost += 1
    return items, cost


def build_layout(
    text: str,
    markup: Any = None,
    *,
    accent_colour: int | None = None,
    select_placeholder: str = "Choose an option…",
    thumbnail: str | None = None,
    button_width: float = DEFAULT_BUTTON_WIDTH,
) -> discord.ui.LayoutView:
    """A never-stored Components V2 view: Container[text blocks…, hairline, ActionRow…]."""
    children, text_cost = _text_items(text, thumbnail)
    rows = _normalize(markup)
    budget = MAX_COMPONENTS - 1 - text_cost  # the container, and the text inside it
    plan = _plan(rows, budget)
    width = card_width([item.buttons for item in plan if item.kind in ("buttons", "entry")], button_width)

    if plan and sum(item.cost for item in plan) < budget:
        children.append(_hairline())

    ids = _IdAllocator()
    select_index = 0
    for item in plan:
        if item.kind == "select":
            children.append(discord.ui.ActionRow(_select(item.buttons, select_index, select_placeholder)))
            select_index += 1
        elif item.kind in ("rule", "divider"):
            children.append(_hairline())
        else:
            if item.kind == "entry":
                children.append(discord.ui.TextDisplay(escape_markdown((item.buttons[0].description or "").strip())))
            labels = _even_labels(item.buttons, width)
            children.append(
                discord.ui.ActionRow(*[_button(b, ids, label=lb) for b, lb in zip(item.buttons, labels, strict=True)])
            )

    view = _StaticLayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_colour=accent_colour))
    view.stop()
    return view


def build_classic_view(markup: Any, *, button_width: float = DEFAULT_BUTTON_WIDTH) -> discord.ui.View | None:
    """Buttons for a classic message (a copied broadcast, a relayed file): at most five rows."""
    rows = _normalize(markup)[:5]
    if not rows:
        return None
    width = card_width(rows, button_width)
    ids = _IdAllocator()
    view = _StaticView(timeout=None)
    for index, row in enumerate(rows):
        for button, label in zip(row, _even_labels(row, width), strict=True):
            item = _button(button, ids, label=label)
            item.row = index
            view.add_item(item)
    view.stop()
    return view


def component_count(view: discord.ui.LayoutView) -> int:
    return len(list(view.walk_children()))


__all__ = [
    "COPY_PREFIX",
    "DANGER_COLOUR",
    "FROZEN_COLOUR",
    "MUTED_COLOUR",
    "NOOP",
    "SELECT_PREFIX",
    "SUCCESS_COLOUR",
    "WARNING_COLOUR",
    "build_classic_view",
    "build_layout",
    "component_count",
    "copy_custom_id",
    "copy_value",
    "split_blocks",
    "strip_dedupe",
    "tone_colour",
]
