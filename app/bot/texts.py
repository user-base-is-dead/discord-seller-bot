from __future__ import annotations

from app.bot.framework.types import LinkPreviewOptions
from app.locales.i18n import t

# The home screen carries links. Discord screens never unfurl them (cards carry no embeds and links
# are rendered in <…>), but every call site still passes this, so the intent stays written down.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def home_body(locale: str, name: str | None) -> str:
    """The one and only text of the main menu screen.

    It lives here rather than in `/start` because the user reaches this screen from at least six
    places — /start, Home, Back, a language change, and cancelling any of the forms. When each of
    those built its own text, only /start ever grew extra lines and every other route quietly
    served a shorter welcome.

    Short on purpose: what each option does is written above its own button, and the official
    channel and group are buttons of their own (shown only when their URL is set), so nothing here
    is wider than the buttons and the card stays one even width.
    """
    return t("welcome.subtitle", locale, name=name or "there") + t("welcome.cta", locale)
