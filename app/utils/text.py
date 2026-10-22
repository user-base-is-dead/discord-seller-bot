from __future__ import annotations

import re
from datetime import datetime

from app.utils.time import as_utc

# Telegram needed a line of U+2800 blanks to stop a short message from squeezing its own inline
# keyboard into a narrow strip. Discord cards are full width, so the pad is empty; the renderer also
# drops any pad line that still reaches it. Kept as a name so every screen that appends it reads the
# same as the Telegram original.
PAD = ""


def escape_html(text: object) -> str:
    """Make arbitrary text safe to drop into an HTML message, without mangling punctuation.

    `html.escape` defaults to `quote=True`, which also rewrites ' and " as numeric entities. That
    matters here because the text going through this is prose somebody typed — a decline reason, a
    payout note — and "the customer's card" came out as "the customer&#x27;s card". Quote escaping
    exists for values placed inside HTML *attributes*; nothing here ever is, so it is switched off.

    Only <, > and & are rewritten. Everything outgoing is HTML that the renderer turns into Discord
    markdown, escaping markdown characters in ordinary text on the way — so a product called
    "*NEW* Pro" still shows its asterisks.
    """
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if text is not None
        else ""
    )


def as_markdown(text: str | None) -> str:
    """Text somebody typed in Discord, sent on with their formatting intact.

    Discord messages are markdown. Wrapped in `<md>`, the renderer passes it through verbatim instead
    of escaping it, so a staff reply in **bold** or a credential in a `code block` arrives exactly as
    written — the equivalent of re-sending a Telegram message's `html_text`.
    """
    if not text:
        return ""
    return f"<md>{escape_html(text)}</md>"


def mention(discord_id: int | str | None) -> str:
    """A clickable user mention. Never pings anybody: every message is sent with mentions disabled."""
    return f"<mention>{discord_id}</mention>" if discord_id else ""


def timestamp(moment: datetime, style: str = "f") -> str:
    """A Discord timestamp. Every reader sees it in their own time zone and language: "f" is the full
    date and time, "R" reads "2 hours ago", "d" is the date alone."""
    return f"<ts>{int(as_utc(moment).timestamp())}:{style}</ts>"


def thumbnail(url: str | None) -> str:
    """Put a picture (a buyer's avatar) beside the top of a card. Nothing when there is no picture."""
    if not url:
        return ""
    return f'<thumb src="{escape_html(url).replace(chr(34), "&quot;")}"/>'


def as_admin_wrote_it(message) -> str:
    """The admin's message, formatting and all, ready to be re-sent.

    Whatever they typed is what the buyer gets: a code block stays a code block, bold stays bold,
    plain text stays plain. `html_text` escapes the literal <, > and & that a password is entitled to
    contain and marks the text as markdown to pass through — the reason this cannot simply be
    `message.text`.
    """
    return (message.html_text or "").strip()


# A card that runs past Discord's 4000-character limit is not refused, it is cut off with "…" — so
# whatever came after the cut simply never arrives. A little under, for the renderer's own trimmings.
MESSAGE_LIMIT = 3900


def message_size(text: str) -> int:
    """How much of the card limit `text` will use once it is rendered, never under the real figure.

    Measured on the markdown the renderer actually produces, not on the HTML going in: the two
    differ in both directions — tags shrink, while every `*`, `_` or `` ` `` in a password gains an
    escaping backslash — so only the rendered text says whether a card will be cut. Counted in
    UTF-16 units, which is never less than the characters the renderer cuts on.
    """
    from app.bot.framework.markdown import to_card

    rendered, _thumbnail = to_card(text, limit=None)
    return len(rendered.encode("utf-16-le")) // 2


def _raw_size(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_VOID_TAGS = {"br", "hr"}


def _hard_cut(text: str, room: int) -> int:
    """The longest prefix of `text` that fits in `room`, backed off so it never ends inside a tag
    or an HTML entity — half of `<code>` or of `&amp;` would render as garbage."""
    used = cut = 0
    for i, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > room:
            break
        cut = i + 1
    head = text[:cut]
    if head.rfind("<") > head.rfind(">"):
        cut = head.rfind("<")
    amp = head.rfind("&")
    if amp > head.rfind(";") and cut - amp <= 10:
        cut = min(cut, amp)
    return max(cut, 1)


def _cut(text: str, room: int) -> list[str]:
    """Cut `text` into pieces of at most `room` as written: on line breaks where possible, anywhere
    if a single line is longer than that."""
    pieces: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        candidate = line if current is None else f"{current}\n{line}"
        if _raw_size(candidate) <= room:
            current = candidate
            continue
        if current is not None:
            pieces.append(current)
        while _raw_size(line) > room:
            cut = _hard_cut(line, room)
            pieces.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        pieces.append(current)
    return pieces


def _balance(pieces: list[str]) -> list[str]:
    """Close every tag a piece leaves open, and reopen it at the start of the next one, so each
    piece renders on its own — a `<pre>` cut in two becomes two code blocks, not stray markup."""
    balanced: list[str] = []
    carried: list[tuple[str, str]] = []
    for piece in pieces:
        text = "".join(opener for _, opener in carried) + piece
        stack: list[tuple[str, str]] = []
        for match in _TAG.finditer(text):
            name = match.group(2).lower()
            if name in _VOID_TAGS:
                continue
            if not match.group(1):
                stack.append((name, match.group(0)))
                continue
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == name:
                    del stack[depth]
                    break
        balanced.append(text + "".join(f"</{name}>" for name, _ in reversed(stack)))
        carried = stack
    return balanced


def _fit(text: str, budget: int) -> list[str]:
    """`text` as pieces that each fit `budget` once rendered — the closing and reopening tags
    included."""
    if message_size(text) <= budget:
        return [text]
    room = budget
    while True:
        pieces = _balance(_cut(text, room))
        if room <= 64 or all(message_size(piece) <= budget for piece in pieces):
            return pieces
        room = room * 3 // 4


def pack_messages(head: str, items: list[str], tail: str = "", *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Lay `head`, the `items` (one per line) and `tail` out over as few messages as fit.

    No card ever goes over `limit`, so the renderer never has to cut one. An item is kept whole
    whenever it fits in a card of its own — a credential split across two messages is two useless
    fragments — and is only cut, on a line break where it can be, when it is bigger than a whole
    card. The head opens the first message and the tail closes the last.
    """
    pieces = [piece for item in items for piece in _fit(item, limit)]
    messages: list[str] = []
    prefix, body = head, []
    for piece in pieces:
        if body and message_size(prefix + "\n".join([*body, piece])) > limit:
            messages.append(prefix + "\n".join(body))
            prefix, body = "", []
        elif not body and prefix and message_size(prefix + piece) > limit:
            messages.extend(_fit(prefix.rstrip("\n"), limit))
            prefix = ""
        body.append(piece)
    last = prefix + "\n".join(body)
    if tail and message_size(last + tail) > limit:
        if last.strip():
            messages.extend(_fit(last.rstrip("\n"), limit))
        messages.extend(_fit(tail.strip("\n"), limit))
    else:
        messages.extend(_fit(last + tail, limit))
    return [message for message in messages if message.strip()]
