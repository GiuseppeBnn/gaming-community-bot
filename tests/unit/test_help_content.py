"""Unit tests for the command help registry rendering (handlers.help_content).

Regression guard for the ``TelegramBadRequest: Unsupported start tag "importo"``
crash: ``usage`` strings contain ``<placeholder>`` tokens that are literal command
syntax, not HTML, and must be escaped before going into a ParseMode.HTML message.
"""

from __future__ import annotations

from handlers import help_content
from handlers.help_content import (
    _COMMANDS,
    render_alduino_reference,
    render_command,
    render_command_or_hint,
)


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


class TestRenderCommandOrHint:
    """Shared by /spiega_comando (private) and the spiega_<cmd> deep-link landing."""

    def test_known_command_returns_manual(self):
        out = render_command_or_hint("daily")
        assert "📘" in out and "/daily" in out

    def test_handles_slash_and_alias(self):
        # normalize() strips the slash; aliases resolve to their canonical doc.
        assert "/daily" in render_command_or_hint("/daily")
        assert "/comandi" in render_command_or_hint("help")  # 'help' is an alias

    def test_unknown_command_gives_not_found_with_suggestion(self):
        out = render_command_or_hint("dailyy")
        assert "non trovato" in out
        assert "/daily" in out  # close match suggested

    def test_admin_only_hidden_from_non_admin(self):
        out = render_command_or_hint("credita", is_admin=False)
        assert "non trovato" in out  # treated as unknown
        assert "🔐" in render_command_or_hint("credita", is_admin=True)


class TestTheGuessGamesAreDiscoverable:
    """Neither game has a command of its own — players arrive from the group
    announcement, admins from the Events hub. So the only place an admin can read
    what they are is the `/eventi` page, and if it does not say so the feature is
    invisible until somebody stumbles on the button."""

    def test_the_events_page_names_both_games(self):
        out = render_command("eventi", is_admin=True)
        assert "Guess The Game" in out and "Sound Quest" in out

    def test_it_says_the_answers_are_judged_loosely_but_not_by_series(self):
        """The single rule players argue about, so it belongs in writing."""
        out = render_command("eventi", is_admin=True)
        assert "GTA SA" in out and "serie" in out.lower()

    def test_it_is_still_hidden_from_non_admins(self):
        """Only admins create rounds; the help surface must not leak the set."""
        assert render_command("eventi", is_admin=False) is None


def test_d20_is_a_public_bare_roll_command():
    page = render_command("d20", is_admin=False)
    assert page is not None
    assert "/d20" in page and "1" in page and "20" in page


class TestAlduinoReference:
    def test_is_generated_from_the_public_catalog(self):
        reference = render_alduino_reference()
        for command in _COMMANDS:
            if not command.admin_only:
                assert f"/{command.name}" in reference

    def test_never_leaks_admin_commands_or_html(self):
        reference = render_alduino_reference()
        assert "/credita" not in reference
        assert "<b>" not in reference
        assert "&lt;" not in reference

    def test_forbids_claiming_side_effects(self):
        assert "non fingere di aver eseguito" in render_alduino_reference()


def esc_token(usage: str) -> str:
    return help_content.esc(usage)
