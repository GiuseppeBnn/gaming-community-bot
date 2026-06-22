"""Tests for handlers/_mentions.py (shared user-mention helper)."""

from __future__ import annotations

import handlers._mentions as m


class TestMention:
    async def test_username_mention(self, session, user_factory):
        await user_factory(tg_id=1, username="mario")
        assert await m.mention(session, 1) == "@mario"

    async def test_link_when_no_username(self, session, user_factory):
        await user_factory(tg_id=2, username=None, full_name="Anna Rossi")
        assert await m.mention(session, 2) == '<a href="tg://user?id=2">Anna Rossi</a>'

    async def test_escapes_full_name(self, session, user_factory):
        await user_factory(tg_id=3, username=None, full_name="<b>hax</b>")
        out = await m.mention(session, 3)
        assert "&lt;b&gt;hax&lt;/b&gt;" in out and "<b>hax" not in out

    async def test_escapes_username(self, session, user_factory):
        await user_factory(tg_id=4, username="a<b")
        assert await m.mention(session, 4) == "@a&lt;b"

    async def test_unknown_user_fallback(self, session):
        assert await m.mention(session, 999) == '<a href="tg://user?id=999">giocatore</a>'
