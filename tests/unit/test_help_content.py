"""Unit tests for the command help registry rendering (handlers.help_content).

Regression guard for the ``TelegramBadRequest: Unsupported start tag "importo"``
crash: ``usage`` strings contain ``<placeholder>`` tokens that are literal command
syntax, not HTML, and must be escaped before going into a ParseMode.HTML message.
"""

from __future__ import annotations

from handlers import help_content
from handlers.help_content import _COMMANDS, render_command


class TestRenderCommandIsHtmlSafe:
    def test_placeholder_usage_is_escaped(self):
        # /trasferisci's usage is "/trasferisci @utente <importo>" — the exact
        # case that crashed Telegram. The raw token must not survive rendering.
        page = render_command("trasferisci", is_admin=True)
        assert page is not None
        assert "<importo>" not in page
        assert "&lt;importo&gt;" in page

    def test_every_command_usage_has_no_raw_placeholder(self):
        # Any command whose usage carries a <placeholder> (n, id, testo, comando…)
        # would trigger the same unknown-tag rejection. Render them all and assert
        # the literal usage angle-bracket tokens never leak unescaped.
        for doc in _COMMANDS:
            if not doc.usage or "<" not in doc.usage:
                continue
            page = render_command(doc.name, is_admin=True)
            assert page is not None, doc.name
            assert esc_token(doc.usage) in page, doc.name
            assert doc.usage not in page, doc.name

    def test_details_html_markup_is_preserved(self):
        # details bodies carry intentional markup and must NOT be escaped.
        page = render_command("ban", is_admin=True)
        assert page is not None
        assert "<b>muto al bot</b>" in page
        assert "&lt;b&gt;" not in page

    def test_admin_command_hidden_from_non_admin(self):
        assert render_command("credita", is_admin=False) is None
        assert render_command("credita", is_admin=True) is not None


def esc_token(usage: str) -> str:
    return help_content.esc(usage)
