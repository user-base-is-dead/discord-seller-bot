"""Telegram-flavoured HTML → Discord markdown.

Every screen in the bot was written as Telegram HTML (`<b>`, `<code>`, `<blockquote>`, `<a href>`).
Rather than rewrite hundreds of strings — and let them drift from the Telegram originals — all text
passes through here on its way out, so the templates stay exactly as they were.

Rules:
  * text between tags is markdown-escaped, because Telegram rendered it literally: a product called
    "*NEW* Pro" must not turn italic, and a user who names themselves "[x](https://…)" must not get
    a masked link into a staff card;
  * `<code>` / `<pre>` content is emitted verbatim inside backticks, as Telegram showed it;
  * `<md>…</md>` is raw Discord markdown written by a person (an admin's stock item or delivery),
    passed through untouched — the Discord equivalent of Telegram's `html_text`;
  * `<mention>ID</mention>` becomes a user mention;
  * an unknown tag is not markup at all (`/broadcast_status <id>`), so it is kept as literal text.

Discord-only tags, for layouts Telegram could not draw:
  * `<h1>` / `<h2>` / `<h3>` → `#` / `##` / `###` headings, `<small>` → `-#` subtext lines;
  * `<hr>` → a divider between two blocks of a card (`SEPARATOR`; see `render.build_layout`);
  * `<ts>UNIX[:STYLE]</ts>` → a Discord timestamp, shown in every reader's own time zone;
  * `<thumb src="URL">` → the card's thumbnail ("bot" stands for the store's own avatar).

Cards (`to_card`) are also *dressed*, so the Telegram templates read well on Discord without being
rewritten: a screen that opens on a bold title line gets that title as a heading, a bold line that
stands alone above a paragraph becomes a section heading, and a line set entirely in italics becomes
subtext. Only the bot's own top-level markup is dressed — text a person wrote (`<md>`), quotes and
code are never restyled.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

TEXT_LIMIT = 4000  # Components V2: total characters across all text displays in one message
CONTENT_LIMIT = 2000  # classic message content / ephemeral notices

# A line holding only this is a divider between two blocks of a card. Private-use characters, like
# the dressing marks below: they are stripped from every incoming text, so nothing a person types
# can pass for one.
SEPARATOR = "\ue000"
_B_OPEN, _B_CLOSE = "\ue001", "\ue002"  # around top-level bold: a heading candidate
_I_OPEN, _I_CLOSE = "\ue003", "\ue004"  # around top-level italics: a subtext candidate
_DRESS_MARKS = _B_OPEN + _B_CLOSE + _I_OPEN + _I_CLOSE
_ALL_MARKS = SEPARATOR + _DRESS_MARKS
_STRIP_ALL = str.maketrans("", "", _ALL_MARKS)
_STRIP_DRESS = str.maketrans("", "", _DRESS_MARKS)

_ZWSP = "\u200b"
_PAD_LINE = re.compile(r"^[\u2800\s]*$")
_URL = re.compile(r"https?://[^\s<>]+", re.I)
# Markdown that means something anywhere in a line.
_INLINE_SPECIAL = re.compile(r"([\\*_~`|\[\]<>])")
# Markdown that only means something at the start of a line: headers, quotes, lists, subtext.
_LINE_START = re.compile(r"^(\s*)(#{1,3}\s|-#\s|>|[-*+]\s)")
_ORDERED = re.compile(r"^(\s*)(\d+)([.)]\s)")
# Placed after a block (code block, quote) so whatever follows starts on its own line — both are
# block-level in Telegram — without doubling a newline that is already there.
_BREAK = "\x00"
_TIMESTAMP = re.compile(r"(\d{1,13})(?::([tTdDfFR]))?")
_RULE = re.compile(r"^[ \t]*[━─═]{6,}[ \t]*$", re.M)

_WRAP = {
    "b": "**",
    "strong": "**",
    "i": "*",
    "em": "*",
    "u": "__",
    "ins": "__",
    "s": "~~",
    "strike": "~~",
    "del": "~~",
    "tg-spoiler": "||",
}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###"}
_RAW = {"code", "pre", "md", "mention", "ts"}
_TRANSPARENT = {"span", "tg-emoji"}
_VOID = {"br", "hr", "thumb"}
_KNOWN = set(_WRAP) | set(_HEADINGS) | _RAW | _TRANSPARENT | _VOID | {"blockquote", "a", "small"}


def _escape_segment(segment: str) -> str:
    return _INLINE_SPECIAL.sub(r"\\\1", segment)


def _escape_line(line: str) -> str:
    # URLs stay byte-for-byte: a backslash inside one would end up in the link Discord opens.
    out: list[str] = []
    position = 0
    for match in _URL.finditer(line):
        out.append(_escape_segment(line[position : match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(_escape_segment(line[position:]))
    escaped = "".join(out)
    escaped = _LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), escaped)
    return _ORDERED.sub(lambda m: m.group(1) + m.group(2) + "\\" + m.group(3), escaped)


def _resolve_breaks(text: str) -> str:
    text = re.sub(_BREAK + r"(?=\n|$)", "", text)
    return text.replace(_BREAK, "\n")


def escape_markdown(text: str) -> str:
    """Escape text so Discord shows it literally."""
    if not text:
        return text
    return "\n".join(_escape_line(line) for line in text.split("\n"))


def _inline_code(content: str) -> str:
    if not content:
        return ""
    if "`" not in content:
        return f"`{content}`"
    # Double backticks with padding carry a literal backtick; a run of two cannot be represented.
    content = content.replace("``", "ˋˋ")
    return f"`` {content} ``"


def _code_block(content: str, language: str = "") -> str:
    content = content.replace("```", "ˋˋˋ").strip("\n")
    return f"```{language}\n{content}\n```"


def _timestamp(content: str) -> str:
    match = _TIMESTAMP.fullmatch(content.strip())
    if match is None:
        return escape_markdown(content)
    return f"<t:{match.group(1)}:{match.group(2) or 'f'}>"


class _Frame:
    __slots__ = ("attrs", "parts", "tag")

    def __init__(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parts: list[str] = []


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = [_Frame("root", {})]
        self.raw_depth = 0  # inside <code>/<pre>/<md>/<mention>/<ts>: no markdown escaping
        self.thumbnail: str | None = None
        # The template set its own headings, so it has laid itself out: it is not dressed.
        self.explicit = False

    @property
    def top(self) -> _Frame:
        return self.stack[-1]

    def _emit(self, text: str) -> None:
        self.top.parts.append(text)

    def _literal(self, raw: str) -> None:
        self._emit(raw if self.raw_depth else escape_markdown(raw))

    # -- parser callbacks ---------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "span" and "tg-spoiler" in (attributes.get("class") or ""):
            tag = "tg-spoiler"
        if tag not in _KNOWN:
            self._literal(self.get_starttag_text() or "")
            return
        if tag == "br":
            self._emit("\n")
            return
        if tag == "hr":
            self._ensure_line_start()
            # A divider splits the card itself, so it only exists between top-level blocks; inside a
            # quote or a code block it is just a paragraph break.
            self._emit(SEPARATOR + _BREAK if len(self.stack) == 1 else "\n")
            return
        if tag == "thumb":
            source = (attributes.get("src") or "").strip()
            if source and self.thumbnail is None:
                self.thumbnail = source
            return
        if tag == "code" and self.top.tag == "pre":
            # <pre><code class="language-x"> — the inner code tag only names the language.
            self.top.attrs["language"] = (attributes.get("class") or "").removeprefix("language-")
            self.stack.append(_Frame("code-in-pre", attributes))
            return
        if tag in _HEADINGS:
            self.explicit = True
        if tag in _RAW:
            self.raw_depth += 1
        self.stack.append(_Frame(tag, attributes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in _VOID:
            self.handle_starttag(tag, attrs)
            return
        if lowered not in _KNOWN:
            self._literal(self.get_starttag_text() or "")
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("hr", "thumb"):
            return  # void: there is nothing to close
        index = next(
            (i for i in range(len(self.stack) - 1, 0, -1) if self._closes(self.stack[i].tag, tag)),
            None,
        )
        if index is None:
            self._literal(f"</{tag}>")
            return
        while len(self.stack) - 1 > index:
            self._close_frame(self.stack.pop())
        self._close_frame(self.stack.pop())

    @staticmethod
    def _closes(frame_tag: str, tag: str) -> bool:
        if frame_tag == tag:
            return True
        if frame_tag == "code-in-pre" and tag == "code":
            return True
        return frame_tag == "tg-spoiler" and tag == "span"

    def handle_data(self, data: str) -> None:
        data = data.translate(_STRIP_ALL)  # an entity can spell a private-use character too
        if self.raw_depth:
            self._emit(data)
            return
        if len(self.stack) == 1:
            # A ruled line (━━━━) is a Telegram template asking for a divider: draw a real one.
            data = _RULE.sub(SEPARATOR, data)
        self._emit(escape_markdown(data))

    # -- closing ------------------------------------------------------------------------------

    def _close_frame(self, frame: _Frame) -> None:
        inner = "".join(frame.parts)
        tag = frame.tag
        if tag in _RAW:
            self.raw_depth = max(0, self.raw_depth - 1)
        if tag in ("code-in-pre", "md") or tag in _TRANSPARENT:
            self._emit(inner)
        elif tag == "code":
            self._emit(_inline_code(inner))
        elif tag == "pre":
            self._ensure_line_start()
            self._emit(_code_block(inner, frame.attrs.get("language") or "") + _BREAK)
        elif tag == "mention":
            user_id = inner.strip()
            self._emit(f"<@{user_id}>" if user_id.isdigit() else escape_markdown(inner))
        elif tag == "ts":
            self._emit(_timestamp(inner))
        elif tag == "blockquote":
            self._ensure_line_start()
            body = _resolve_breaks(inner).strip("\n")
            lines = body.split("\n") if body else [""]
            quoted = "\n".join(f"> {line}" if line.strip() else f"> {_ZWSP}" for line in lines)
            self._emit(quoted + _BREAK)
        elif tag in _HEADINGS:
            line = " ".join(_resolve_breaks(inner).split("\n")).strip()
            if line:
                self._ensure_line_start()
                self._emit(f"{_HEADINGS[tag]} {line}{_BREAK}")
        elif tag == "small":
            body = _resolve_breaks(inner).strip("\n")
            lines = [line.strip() for line in body.split("\n") if line.strip()]
            if lines:
                self._ensure_line_start()
                self._emit("\n".join(f"-# {line}" for line in lines) + _BREAK)
        elif tag == "a":
            self._emit(self._link(frame.attrs.get("href") or "", inner))
        elif tag in _WRAP:
            marks = ("", "")
            if self.top.tag == "root":  # only the template's own top-level markup is ever dressed
                if tag in ("b", "strong"):
                    marks = (_B_OPEN, _B_CLOSE)
                elif tag in ("i", "em"):
                    marks = (_I_OPEN, _I_CLOSE)
            self._emit(self._wrap(inner, _WRAP[tag], marks))
        else:
            self._emit(inner)

    def _wrap(self, inner: str, marker: str, marks: tuple[str, str] = ("", "")) -> str:
        if not inner.strip():
            return inner
        # Markers must hug the text: "** bold**" does not render in Discord.
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()) :]
        core = inner.strip()
        previous = (self.top.parts[-1] if self.top.parts else "").rstrip(_ALL_MARKS)
        joiner = _ZWSP if previous and previous[-1] in "*_~|" and not lead else ""
        return f"{joiner}{lead}{marks[0]}{marker}{core}{marker}{marks[1]}{trail}"

    @staticmethod
    def _link(href: str, label: str) -> str:
        href = html.unescape(href.strip())
        user = re.fullmatch(r"(?:tg://user\?id=|discord://user/)(\d+)", href)
        if user:
            return f"<@{user.group(1)}>"
        if not re.match(r"https?://", href, re.I):
            return label
        shown = re.sub(r"\\(.)", r"\1", label).strip()
        # Discord refuses to mask a link behind text that itself looks like an address, so an
        # address-like label is shown as the bare link instead (clickable, no preview card).
        if not shown or (" " not in shown and ("." in shown or "/" in shown)):
            return f"<{href}>"
        return f"[{label}](<{href}>)"

    def _ensure_line_start(self) -> None:
        emitted = "".join(self.top.parts)
        if emitted and not emitted.endswith(("\n", _BREAK)):
            self._emit("\n")

    def result(self) -> str:
        self.close()
        while len(self.stack) > 1:
            self._close_frame(self.stack.pop())
        return _resolve_breaks("".join(self.stack[0].parts))


def _convert(text: str) -> tuple[str, str | None, bool]:
    converter = _Converter()
    converter.feed(text.translate(_STRIP_ALL))
    return converter.result(), converter.thumbnail, converter.explicit


# -- dressing (cards only) ------------------------------------------------------------------------

# What may stand in front of a heading: an emoji or two. Never a bullet, a dash or a word —
# "• **x**" is a list item and "Total: **x**" a value, not headings.
_LEAD = r"[^\w\s\\*_~`|<>\[\]()#.,:;!?'\"•·‣◦▪▸\-–—" + _ALL_MARKS + r"]+"
_BOLD = rf"^\u200b?((?:{_LEAD}\s*)?){_B_OPEN}\*\*([^{_ALL_MARKS}\n]+?)\*\*{_B_CLOSE}"
_BOLD_LINE = re.compile(rf"{_BOLD}\s*$")
# The opening line may carry a short tail after a dash: "➕ **Add Product** — step 1 of 9".
_TITLE_LINE = re.compile(rf"{_BOLD}(\s+[—–·-]\s+[^{_ALL_MARKS}\n*]{{1,40}}?)?\s*$")
_ITALIC_LINE = re.compile(rf"^\u200b?{_I_OPEN}\*([^{_ALL_MARKS}\n]+?)\*{_I_CLOSE}\s*$")


def _has_text(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped not in (SEPARATOR, _ZWSP)


def _dress(markdown: str) -> str:
    """Give a Telegram-style layout Discord's typography.

    * the screen's opening line, when it is nothing but a bold title (and perhaps a short tail after
      a dash), becomes a `##` heading;
    * any later line that is nothing but bold and sits directly on top of more text becomes a `###`
      section heading;
    * a line set entirely in italics (a footnote, a timestamp, a tagline) becomes `-#` subtext.

    A one-line message is left alone: a heading with nothing under it is just a louder notice.
    """
    lines = markdown.split("\n")
    filled = [i for i, line in enumerate(lines) if _has_text(line)]
    if len(filled) < 2:
        return markdown
    for i in filled:
        line = lines[i]
        if i == filled[0]:
            title = _TITLE_LINE.match(line)
            if title is not None:
                lines[i] = f"## {title.group(1)}{title.group(2)}{title.group(3) or ''}"
                continue
        bold = _BOLD_LINE.match(line)
        if bold is not None:
            if i != filled[0] and i + 1 < len(lines) and _has_text(lines[i + 1]):
                lines[i] = f"### {bold.group(1)}{bold.group(2)}"
            continue
        italic = _ITALIC_LINE.match(line)
        if italic is not None:
            lines[i] = f"-# {italic.group(1)}"
    return "\n".join(lines)


_CLASSIC_RULE = "━━━━━━━━━━━━━━━━━━"


def _flatten(markdown: str) -> str:
    """Classic messages have no blocks: a divider is drawn as a ruled line there."""
    markdown = markdown.translate(_STRIP_DRESS)
    lines = [_CLASSIC_RULE if line.strip() == SEPARATOR else line for line in markdown.split("\n")]
    return "\n".join(lines).replace(SEPARATOR, "")


# -- public API -----------------------------------------------------------------------------------


def html_to_markdown(text: str | None, *, limit: int | None = TEXT_LIMIT) -> str:
    if not text:
        return tidy("", limit=limit)
    markdown, _thumbnail, _explicit = _convert(text)
    return tidy(_flatten(markdown), limit=limit)


def plain_to_markdown(text: str | None, *, limit: int | None = TEXT_LIMIT) -> str:
    return tidy(escape_markdown((text or "").translate(_STRIP_ALL)), limit=limit)


def _mode(parse_mode: str | None) -> str | None:
    return str(parse_mode).upper() if parse_mode is not None else None


def to_markdown(text: str | None, parse_mode: str | None = "HTML", *, limit: int | None = TEXT_LIMIT) -> str:
    """Render outgoing text. HTML is the default, like the Telegram bot's DefaultBotProperties."""
    mode = _mode(parse_mode)
    if mode == "HTML":
        return html_to_markdown(text, limit=limit)
    if mode is not None and mode.startswith("MARKDOWN"):
        return tidy((text or "").translate(_STRIP_ALL), limit=limit)
    return plain_to_markdown(text, limit=limit)


def to_card(
    text: str | None, parse_mode: str | None = "HTML", *, limit: int | None = TEXT_LIMIT
) -> tuple[str, str | None]:
    """A card's text and its thumbnail source.

    The text keeps its `SEPARATOR` lines, which `render.build_layout` turns into dividers, and is
    dressed (see `_dress`) unless the template set headings of its own. Only HTML is dressed:
    markdown and plain text are somebody's own words.
    """
    if _mode(parse_mode) != "HTML":
        return to_markdown(text, parse_mode, limit=limit), None
    markdown, thumbnail, explicit = _convert(text or "")
    if not explicit:
        markdown = _dress(markdown)
    return tidy(markdown.translate(_STRIP_DRESS), limit=limit), thumbnail


def tidy(text: str, *, limit: int | None = TEXT_LIMIT) -> str:
    """Drop Telegram's bubble-widening pad lines and trailing blank space; enforce Discord's limit."""
    lines = [line.rstrip("\u2800") for line in text.split("\n")]
    while lines and _PAD_LINE.match(lines[-1]):
        lines.pop()
    result = "\n".join(lines).rstrip()
    if limit is not None and len(result) > limit:
        result = result[: limit - 1].rstrip() + "…"
    return result or _ZWSP


__all__ = [
    "CONTENT_LIMIT",
    "SEPARATOR",
    "TEXT_LIMIT",
    "escape_markdown",
    "html_to_markdown",
    "plain_to_markdown",
    "tidy",
    "to_card",
    "to_markdown",
]
