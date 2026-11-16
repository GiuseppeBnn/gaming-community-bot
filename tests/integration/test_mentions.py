"""Tests for handlers/_mentions.py (shared user-mention helper)."""

from __future__ import annotations

import handlers._mentions as m
import pytest


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


async def test_batch_mentions_match_single_mentions_and_escape_html(session, user_factory, monkeypatch):
    from unittest.mock import AsyncMock

    await user_factory(tg_id=1, username="a<b")
    await user_factory(tg_id=2, username=None, full_name="Mario & Rossi")
    execute = AsyncMock(wraps=session.execute)
    monkeypatch.setattr(session, "execute", execute)
    batch = await m.mention_many(session, [1, 2, 999])
    assert execute.await_count == 1
    assert batch == {tg_id: await m.mention(session, tg_id) for tg_id in [1, 2, 999]}
    assert await m.mention_many(session, []) == {}


async def test_preferred_name_is_clickable_even_when_username_exists(session, user_factory):
    await user_factory(tg_id=1, username="unrelated_handle", full_name="Anna & Mario")
    names = await m.mention_many(session, [1, 99], prefer_name=True)
    assert names[1] == '<a href="tg://user?id=1">Anna &amp; Mario</a>'
    assert names[99] == '<a href="tg://user?id=99">giocatore</a>'
    assert await m.mention(session, 1) == "@unrelated_handle"  # Other features stay unchanged.


@pytest.mark.parametrize("name", [" ", "\u200b", "\u2800"])
@pytest.mark.parametrize("username,expected", [("handle", "@handle"), (None, "giocatore")])
async def test_preferred_name_never_renders_an_empty_link(session, user_factory, name, username, expected):
    await user_factory(tg_id=1, username=username, full_name=name)
    assert (await m.mention_many(session, [1], prefer_name=True))[1] == f'<a href="tg://user?id=1">{expected}</a>'
