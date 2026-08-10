from types import SimpleNamespace

import pytest
from telegram.constants import ParseMode

from atlas.bot.outbound import _deliver, _html, _plain


def test_markdown_link_survives_telegram_rendering():
    url = "http://localhost:8000/oauth/google/start?state=abc_123-xyz"

    rendered = _html(f"Connect using [this link]({url}) so I can edit the sheet.")

    assert rendered == (
        'Connect using <a href="http://localhost:8000/oauth/google/start?state=abc_123-xyz">'
        "this link</a> so I can edit the sheet."
    )


def test_plain_fallback_exposes_link_destination():
    url = "https://atlas.example/oauth/google/start?state=abc"

    assert _plain(f"Use [this link]({url}).") == f"Use this link ({url})."


def test_bold_and_link_render_together():
    assert _html("**Connect** with [Google](https://accounts.google.com).") == (
        '<b>Connect</b> with <a href="https://accounts.google.com">Google</a>.'
    )


def test_non_web_markdown_link_stays_inert():
    assert _html("Open [this](tg://user?id=1)") == "Open [this](tg://user?id=1)"


def test_html_escapes_model_text_and_link_attributes():
    assert _html('A < B & [open](https://example.com/?a=1&b=2)') == (
        'A &lt; B &amp; <a href="https://example.com/?a=1&amp;b=2">open</a>'
    )


@pytest.mark.asyncio
async def test_delivery_uses_telegram_html_rendering_first():
    calls = []

    async def action(rendered, mode):
        calls.append((rendered, mode))
        return SimpleNamespace(message_id=1)

    await _deliver(action, "Use [Google](https://google.com)")

    assert calls == [
        ('Use <a href="https://google.com">Google</a>', ParseMode.HTML)
    ]
