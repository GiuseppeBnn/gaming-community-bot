"""Every setting must be read by something.

A config field nobody reads is worse than a missing one: it is a written promise.
`BET_DEFAULT_WINDOW_MINUTES` sat in `Settings` and in the README table — described
as «preset suggerito + fallback della finestra puntate» — while no line of the bot
ever looked at it. Set it in `.env`, restart, nothing changes, and the debugging
goes somewhere else entirely. `quiz_default_prize` was the same story with a
«legacy» label on it.

Removing them is only half the fix; without this test the next one arrives the same
way, by surviving a refactor that deleted its last reader.

The check is deliberately a text search for the field **name** rather than for
`settings.<name>`: several settings are reached through `getattr(settings, attr)`
with the attribute name coming from a table (the quiz prize steps do this), and a
stricter pattern would fail those for no reason. A name that appears nowhere in
`src/` outside its own declaration cannot be reached by any spelling.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
CONFIG = SRC / "config_data" / "config.py"

# Indented four spaces, i.e. a field of the Settings class rather than a
# module-level name. Matches both `    foo: int = 3` and the required fields that
# have no default at all (`    bot_token: str`) — an unread required setting is
# just as dead as an unread optional one.
_FIELD = re.compile(r"^ {4}([a-z_][a-z0-9_]*)\s*:\s*[^\s=]", re.M)


def _settings_fields() -> list[str]:
    return sorted(set(_FIELD.findall(CONFIG.read_text(encoding="utf-8"))))


def test_the_field_scan_actually_finds_fields():
    """Guards the guard: a regex that silently matches nothing would make the test
    below pass forever while checking absolutely nothing."""
    fields = _settings_fields()
    assert len(fields) > 30, f"only {len(fields)} settings found — the regex broke"
    assert "bot_token" in fields


def test_every_setting_is_read_somewhere():
    sources = [
        p for p in SRC.rglob("*.py") if p != CONFIG
    ]
    texts = {p: p.read_text(encoding="utf-8") for p in sources}

    unread = [
        field
        for field in _settings_fields()
        if not any(field in text for text in texts.values())
    ]

    assert not unread, (
        "settings nobody reads (delete them, or wire them up — but do not leave "
        f"them documented as working): {unread}"
    )
