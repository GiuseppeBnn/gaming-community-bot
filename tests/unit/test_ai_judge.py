"""`ai_service.judge_equivalence` — the deterministic half of the AI service.

`generate_completion` is a creative-writing call: high temperature, free text,
and a failure just means a joke doesn't land. This one decides whether somebody
gets paid, so it is the opposite call — temperature 0, a schema the model cannot
step outside of, and a parse that refuses anything it does not fully understand.

Three properties are pinned because they are what a game can be built on:

  * **it never returns a maybe** — a body that isn't a clean boolean raises, and
    the caller turns that into "not proven correct";
  * **it retries once on a rate limit** — 429 is the *expected* failure on the
    Groq free tier, and burning a player's attempt on it would be our bug
    charged to them;
  * **it asks for constrained decoding** — the request carries the strict JSON
    schema, so the guarantee comes from the API and not from the prompt asking
    nicely.
"""

from __future__ import annotations

import json

import aiohttp
import pytest

from services import ai_service


class _FakeResponse:
    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records the outgoing payloads and replays a scripted list of responses."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.payloads: list[dict] = []

    def post(self, url, *, headers, json):  # noqa: A002 — aiohttp's own kwarg name
        self.payloads.append(json)
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _verdict_body(value: bool) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"corretta": value})}}]}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The retry delay is real in production; paying it in the suite is not."""
    monkeypatch.setattr(ai_service, "_JUDGE_RETRY_DELAY", 0)


@pytest.fixture
def groq(monkeypatch):
    """Installs a fake aiohttp session; returns a factory that scripts responses."""
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")

    def _script(*responses: _FakeResponse) -> _FakeSession:
        session = _FakeSession(list(responses))
        monkeypatch.setattr(ai_service.aiohttp, "ClientSession", lambda **kw: session)
        return session

    return _script


class TestVerdict:
    async def test_a_true_verdict_comes_back_as_true(self, groq):
        groq(_FakeResponse(200, _verdict_body(True)))
        assert await ai_service.judge_equivalence("sys", "gta sa") is True

    async def test_a_false_verdict_comes_back_as_false(self, groq):
        groq(_FakeResponse(200, _verdict_body(False)))
        assert await ai_service.judge_equivalence("sys", "tetris") is False


class TestTheRequest:
    async def test_it_asks_for_constrained_decoding(self, groq):
        """The schema is what makes the verdict un-mistakable; a prompt asking
        politely for JSON is not the same guarantee."""
        session = groq(_FakeResponse(200, _verdict_body(True)))

        await ai_service.judge_equivalence("sys", "x")

        fmt = session.payloads[0]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["schema"]["properties"] == {
            "corretta": {"type": "boolean"}
        }

    async def test_the_schema_has_no_free_text_field(self):
        """Nothing in the reply may be able to carry the correct answer back."""
        assert set(ai_service.JUDGE_SCHEMA["schema"]["properties"]) == {"corretta"}
        assert ai_service.JUDGE_SCHEMA["schema"]["additionalProperties"] is False

    async def test_it_is_deterministic(self, groq):
        """Two players typing the same thing must not get different verdicts."""
        session = groq(_FakeResponse(200, _verdict_body(True)))

        await ai_service.judge_equivalence("sys", "x")

        assert session.payloads[0]["temperature"] == 0

    async def test_it_uses_the_judge_model_not_the_entertainment_one(
        self, groq, monkeypatch
    ):
        monkeypatch.setattr(ai_service.settings, "groq_model", "llama-entertainment")
        monkeypatch.setattr(ai_service.settings, "groq_judge_model", "judge-model")
        session = groq(_FakeResponse(200, _verdict_body(True)))

        await ai_service.judge_equivalence("sys", "x")

        assert session.payloads[0]["model"] == "judge-model"

    async def test_the_player_text_goes_in_the_user_message_not_the_system_one(
        self, groq
    ):
        """Anything user-controlled that lands in the system prompt is an
        instruction; in the user message it is data."""
        session = groq(_FakeResponse(200, _verdict_body(True)))

        await ai_service.judge_equivalence("le regole", "gta sa")

        messages = session.payloads[0]["messages"]
        assert messages[0] == {"role": "system", "content": "le regole"}
        assert messages[1] == {"role": "user", "content": "gta sa"}


class TestFailures:
    async def test_a_missing_api_key_raises_without_calling_out(self, monkeypatch):
        monkeypatch.setattr(ai_service.settings, "groq_api_key", "")
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")

    async def test_a_rate_limit_is_retried_once(self, groq):
        """429 is the expected failure on the free tier. Giving up on the first
        one charges our rate limit to the player's attempt count."""
        session = groq(_FakeResponse(429, "rate limited"),
                       _FakeResponse(200, _verdict_body(True)))

        assert await ai_service.judge_equivalence("sys", "x") is True
        assert len(session.payloads) == 2

    async def test_two_rate_limits_in_a_row_raise(self, groq):
        groq(_FakeResponse(429, "rate limited"), _FakeResponse(429, "rate limited"))
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")

    async def test_a_server_error_is_retried_too(self, groq):
        session = groq(_FakeResponse(503, "unavailable"),
                       _FakeResponse(200, _verdict_body(False)))

        assert await ai_service.judge_equivalence("sys", "x") is False
        assert len(session.payloads) == 2

    async def test_a_client_error_is_not_retried(self, groq):
        """A 400 means the request is wrong; sending it again only burns quota."""
        session = groq(_FakeResponse(400, "bad model"))
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")
        assert len(session.payloads) == 1

    @pytest.mark.parametrize("content", [
        "certo che sì",          # prose instead of JSON
        '{"corretta": "sì"}',    # right key, wrong type
        '{"corretta": 1}',       # an int is not a boolean, even in Python
        '{"altro": true}',       # valid JSON, wrong shape
        '{"corretta": null}',
    ])
    async def test_anything_that_is_not_a_clean_boolean_raises(self, groq, content):
        """There is no "probably correct". The caller turns a raise into
        `unverified`, which does not pay and does not burn an attempt."""
        groq(_FakeResponse(200, {"choices": [{"message": {"content": content}}]}))
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")

    async def test_a_malformed_envelope_raises(self, groq):
        groq(_FakeResponse(200, {"choices": []}))
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")


class TestTransportFailures:
    """The network branches, which the status-code tests never reach."""

    def _raising_session(self, monkeypatch, exc: Exception, *, then=None):
        calls = {"n": 0}

        class _S:
            def post(self, url, *, headers, json):  # noqa: A002
                calls["n"] += 1
                if calls["n"] == 1 or then is None:
                    raise exc
                return then

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

        monkeypatch.setattr(ai_service.aiohttp, "ClientSession", lambda **kw: _S())
        return calls

    async def test_a_timeout_is_retried_then_raises(self, monkeypatch):
        monkeypatch.setattr(ai_service.settings, "groq_api_key", "k")
        calls = self._raising_session(monkeypatch, TimeoutError())

        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")

        assert calls["n"] == 2, "a timeout gets a second chance before giving up"

    async def test_a_timeout_that_clears_on_the_retry_returns_the_verdict(
        self, monkeypatch
    ):
        monkeypatch.setattr(ai_service.settings, "groq_api_key", "k")
        self._raising_session(monkeypatch, TimeoutError(),
                              then=_FakeResponse(200, _verdict_body(True)))

        assert await ai_service.judge_equivalence("sys", "x") is True

    async def test_a_network_error_is_retried_then_raises(self, monkeypatch):
        monkeypatch.setattr(ai_service.settings, "groq_api_key", "k")
        calls = self._raising_session(monkeypatch, aiohttp.ClientError())

        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("sys", "x")

        assert calls["n"] == 2
