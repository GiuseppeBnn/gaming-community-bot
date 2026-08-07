"""inline_mode — the inline user picker. Fake query objects mirror the repo's
duck-typed convention (see test_profile_public._FakeMsg)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import handlers.inline_mode as inline_mode


class _FakeInlineQuery:
    def __init__(self, text: str = ""):
        self.from_user = SimpleNamespace(id=111, username="cercante", full_name="Cerco")
        self.query = text
        self.answers = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


@pytest.fixture(autouse=True)
def _isolate():
    inline_mode.clear_cache()
    yield
    inline_mode.clear_cache()


@pytest.fixture
async def seeded(session, user_factory):
    await user_factory(tg_id=1, username="giu", full_name="Giuseppe", coins=999)
    await user_factory(tg_id=2, username="gio", full_name="Giovanni", coins=5)
    await user_factory(tg_id=3, username="mario", full_name="Mario Rossi", coins=50)
    await user_factory(tg_id=4, username=None, full_name="SenzaHandler", coins=7)


async def _call(text: str = "gi", session=None):
    query = _FakeInlineQuery(text)
    await inline_mode.user_picker(query, session)
    return query.answers[0]


class TestQueryParsing:
    async def test_empty_query_returns_a_hint(self, seeded, session):
        ans = await _call("", session)

        assert len(ans["results"]) == 1
        assert "lettere" in ans["results"][0].title

    async def test_one_char_query_returns_a_hint(self, seeded, session):
        ans = await _call("g", session)

        assert len(ans["results"]) == 1
        assert "altre lettere" in ans["results"][0].title

    async def test_leading_at_and_whitespace_are_stripped(self, seeded, session):
        ans = await _call("  @gio  ", session)

        # match on "gio"
        assert any(r.id == "2" for r in ans["results"])


class TestMatching:
    async def test_partial_match_on_username(self, seeded, session):
        # "gi" è sottostringa sia di "giu" che di "gio" (username) e di "Giuseppe".
        ans = await _call("gi", session)

        ids = {r.id for r in ans["results"]}
        assert "1" in ids and "2" in ids and "3" not in ids and "4" not in ids

    async def test_partial_match_on_full_name(self, seeded, session):
        ans = await _call("mario ross", session)

        assert any(r.id == "3" for r in ans["results"])

    async def test_no_matches_returns_a_hint(self, seeded, session):
        ans = await _call("zzz", session)

        assert len(ans["results"]) == 1
        assert "Nessun giocatore" in ans["results"][0].title

    async def test_results_are_capped(self, seeded, session, user_factory):
        for i in range(5, 25):
            await user_factory(tg_id=i, username=f"gius{i}", full_name=f"Utente {i}", coins=0)
        ans = await _call("gius", session)

        assert len(ans["results"]) <= 20


class TestArticleContent:
    async def test_article_contains_the_profile_card_with_balance(self, seeded, session):
        ans = await _call("giu", session)

        content = ans["results"][0].input_message_content
        assert "999" in content.message_text       # saldo incluso
        assert "CoInn" in content.message_text
        assert content.parse_mode == "HTML"

    async def test_title_describes_the_player(self, seeded, session):
        ans = await _call("giu", session)

        r = ans["results"][0]
        assert "Giuseppe" in r.title
        assert "giu" in r.description


class TestCaching:
    async def test_identical_query_is_cached(self, seeded, session, monkeypatch):
        # `_search_users` è l'alias di modulo che il handler chiama: si patcha quello.
        calls = {"n": 0}
        real = inline_mode._search_users

        async def counting(session, q, limit=15):
            calls["n"] += 1
            return await real(session, q, limit=limit)

        monkeypatch.setattr(inline_mode, "_search_users", counting)

        await _call("giu", session)
        assert calls["n"] == 1
        assert inline_mode._cache_size() == 1

        await _call("giu", session)
        assert calls["n"] == 1, "seconda query identica → cache, nessuna search"

    async def test_different_query_bypasses_cache(self, seeded, session, monkeypatch):
        calls = {"n": 0}
        real = inline_mode._search_users

        async def counting(session, q, limit=15):
            calls["n"] += 1
            return await real(session, q, limit=limit)

        monkeypatch.setattr(inline_mode, "_search_users", counting)
        await _call("giu", session)
        await _call("gio", session)

        assert calls["n"] == 2

    async def test_short_or_empty_queries_are_not_cached(self, seeded, session):
        await _call("g", session)
        assert inline_mode._cache_size() == 0
