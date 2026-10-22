from __future__ import annotations

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
# whatever came after the cut simply never arrives. Counted here on the HTML as written, before it
# is rendered to markdown, with headroom for what the rendering adds.
MESSAGE_LIMIT = 3500


def pack_messages(head: str, items: list[str], tail: str = "", *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Lay `head`, the `items` (one per line) and `tail` out over as few messages as fit.

    An item is never cut in half: a credential split across two messages is two useless fragments.
    The head opens the first message and the tail closes the last; continuation messages carry
    items only.
    """
    messages: list[str] = []
    prefix, body = head, []
    for item in items:
        if body and len(prefix) + len("\n".join([*body, item])) > limit:
            messages.append(prefix + "\n".join(body))
            prefix, body = "", []
        body.append(item)
    last = prefix + "\n".join(body)
    if tail and last and len(last) + len(tail) > limit:
        messages.append(last)
        last = tail.lstrip("\n")
    else:
        last += tail
    messages.append(last)
    return messages
