from __future__ import annotations

from datetime import datetime

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_service, alduino_chat
from services.alduino_chat import AlduinoAIError, DialogueTurn, GeneratedReply
from services.public_event import PublicEvent


def _response(text: str = "Ciao.", interaction_id: str = "turn-1", status: str = "completed"):
    return {
        "id": interaction_id,
        "status": status,
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
    }


@pytest.fixture
def gemini_settings(monkeypatch):
    monkeypatch.setattr(alduino_chat.settings, "alduino_provider", "gemini")
    monkeypatch.setattr(alduino_chat.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(alduino_chat.settings, "alduino_gemini_model", "gemini-chat-test")
    monkeypatch.setattr(alduino_chat.settings, "alduino_thinking_level", "minimal")
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", False)


async def test_gemini_interaction_has_chat_optimized_contract(gemini_settings):
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=200, payload=_response())
        result = await alduino_chat.generate_reply(
            system_prompt="persona + manuale",
            current="come va?",
            live_context="evento live",
            group_context="non inviare al provider legacy",
        )

    assert result == GeneratedReply("Ciao.", "gemini", "turn-1")
    request = next(iter(mocked.requests.values()))[0]
    sent = request.kwargs["json"]
    assert sent["model"] == "gemini-chat-test"
    assert sent["system_instruction"] == "persona + manuale"
    assert sent["store"] is True
    assert sent["generation_config"] == {
        "max_output_tokens": 280,
        "thinking_level": "minimal",
        "thinking_summaries": "none",
    }
    assert "evento live" in sent["input"]
    assert "non inviare al provider legacy" not in sent["input"]
    assert request.kwargs["headers"]["Api-Revision"] == alduino_chat._API_REVISION


async def test_gemini_continues_server_side_branch_without_resending_history(
    gemini_settings,
):
    parent = type("Parent", (), {
        "provider": "gemini",
        "provider_interaction_id": "parent-interaction",
        "history_json": alduino_chat.encode_history((
            DialogueTurn("messaggio vecchio", "risposta vecchia"),
        )),
    })()
    with aioresponses() as mocked:
        mocked.post(
            alduino_chat.GEMINI_INTERACTIONS_URL,
            status=200,
            payload=_response(interaction_id="child"),
        )
        result = await alduino_chat.generate_reply(
            system_prompt="s", current="e perché?", parent=parent,
        )

    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert result.interaction_id == "child"
    assert sent["previous_interaction_id"] == "parent-interaction"
    assert "messaggio vecchio" not in sent["input"]
    assert "e perché?" in sent["input"]


async def test_expired_server_state_rebuilds_the_local_branch(gemini_settings):
    parent = type("Parent", (), {
        "provider": "gemini",
        "provider_interaction_id": "expired",
        "history_json": alduino_chat.encode_history((DialogueTurn("prima", "risposta"),)),
    })()
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=404, body="gone")
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=200, payload=_response())
        await alduino_chat.generate_reply(system_prompt="s", current="dopo", parent=parent)

    requests = list(mocked.requests.values())[0]
    assert len(requests) == 2
    assert requests[0].kwargs["json"]["previous_interaction_id"] == "expired"
    retry = requests[1].kwargs["json"]
    assert "previous_interaction_id" not in retry
    assert "UTENTE: prima" in retry["input"]
    assert "ALDUINO: risposta" in retry["input"]


async def test_retryable_gemini_failure_recovers_once(gemini_settings, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(alduino_chat.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=503)
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=200, payload=_response())
        result = await alduino_chat.generate_reply(system_prompt="s", current="u")

    assert result.provider == "gemini"


@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientConnectionError("down")])
async def test_gemini_transport_failure_falls_back_to_groq(
    gemini_settings, monkeypatch, failure,
):
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", True)

    async def no_sleep(_):
        return None

    async def groq(*args, **kwargs):
        return "risposta di scorta"

    monkeypatch.setattr(alduino_chat.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ai_service, "generate_groq_completion", groq)
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, exception=failure)
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, exception=failure)
        result = await alduino_chat.generate_reply(system_prompt="s", current="u")

    assert result == GeneratedReply("risposta di scorta", "groq")


async def test_missing_both_providers_is_one_normalized_error(gemini_settings, monkeypatch):
    monkeypatch.setattr(alduino_chat.settings, "gemini_api_key", "")
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", True)

    async def down(*args, **kwargs):
        raise ai_service.AIServiceError("down")

    monkeypatch.setattr(ai_service, "generate_groq_completion", down)
    with pytest.raises(AlduinoAIError, match="Groq unavailable"):
        await alduino_chat.generate_reply(system_prompt="s", current="u")


async def test_openrouter_chat_uses_zdr_deepseek_route_and_group_context(monkeypatch):
    captured = {}

    async def openrouter(system, user, **kwargs):
        captured.update(system=system, user=user, **kwargs)
        return "sono nel discorso"

    monkeypatch.setattr(alduino_chat.settings, "alduino_provider", "openrouter")
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", False)
    monkeypatch.setattr(
        alduino_chat.settings, "openrouter_chat_models", "deepseek/first,deepseek/second",
    )
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", openrouter)
    result = await alduino_chat.generate_reply(
        system_prompt="persona", current="che ne pensi?", group_context="Mario: Elden Ring",
    )
    assert result == GeneratedReply("sono nel discorso", "openrouter")
    assert captured["models"] == ("deepseek/first", "deepseek/second")
    assert captured["require_zdr"] is True
    assert captured["feature"] == "alduino_chat"
    assert "Mario: Elden Ring" in captured["user"]


async def test_openrouter_failure_has_explicit_groq_fallback(monkeypatch):
    captured = {}

    async def down(*args, **kwargs):
        raise ai_service.AIServiceError("down")

    async def groq(_system, prompt, **kwargs):
        captured["prompt"] = prompt
        return "scorta"

    monkeypatch.setattr(alduino_chat.settings, "alduino_provider", "openrouter")
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", True)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", down)
    monkeypatch.setattr(ai_service, "generate_groq_completion", groq)
    assert await alduino_chat.generate_reply(
        system_prompt="s", current="u", group_context="messaggio ambientale privato",
    ) == GeneratedReply("scorta", "groq")
    assert "messaggio ambientale privato" not in captured["prompt"]


async def test_openrouter_failure_can_be_fail_closed(monkeypatch):
    async def down(*args, **kwargs):
        raise ai_service.AIServiceError("down")

    monkeypatch.setattr(alduino_chat.settings, "alduino_provider", "openrouter")
    monkeypatch.setattr(alduino_chat.settings, "alduino_fallback_to_groq", False)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", down)
    with pytest.raises(AlduinoAIError, match="OpenRouter unavailable"):
        await alduino_chat.generate_reply(system_prompt="s", current="u")


async def test_incomplete_or_empty_gemini_response_is_rejected(gemini_settings):
    with aioresponses() as mocked:
        mocked.post(
            alduino_chat.GEMINI_INTERACTIONS_URL,
            status=200,
            payload=_response(status="incomplete"),
        )
        with pytest.raises(AlduinoAIError, match="incomplete"):
            await alduino_chat.generate_reply(system_prompt="s", current="u")

    with aioresponses() as mocked:
        mocked.post(
            alduino_chat.GEMINI_INTERACTIONS_URL,
            status=200,
            payload=_response(text=""),
        )
        with pytest.raises(AlduinoAIError, match="empty"):
            await alduino_chat.generate_reply(system_prompt="s", current="u")


async def test_non_object_gemini_response_is_rejected(gemini_settings):
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=200, payload=[])
        with pytest.raises(AlduinoAIError, match="malformed response"):
            await alduino_chat.generate_reply(system_prompt="s", current="u")


async def test_non_stale_parent_error_is_not_rebuilt(gemini_settings, monkeypatch):
    parent = type("Parent", (), {
        "provider": "gemini",
        "provider_interaction_id": "valid-parent",
        "history_json": "[]",
    })()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(alduino_chat.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=500)
        mocked.post(alduino_chat.GEMINI_INTERACTIONS_URL, status=500)
        with pytest.raises(AlduinoAIError, match="status 500"):
            await alduino_chat.generate_reply(
                system_prompt="s", current="u", parent=parent,
            )

    # 5xx gets the normal transport retry, never a third request rebuilt from
    # local history. Only an actually stale parent (400/404) needs rebuilding.
    assert len(next(iter(mocked.requests.values()))) == 2


def test_history_is_newest_first_bounded_by_turns_and_characters():
    previous = tuple(DialogueTurn(str(i) * 100, "a" * 100) for i in range(5))
    result = alduino_chat.bounded_history(
        previous, DialogueTurn("nuovo", "finale"), max_turns=3, max_chars=420,
    )

    assert len(result) == 3
    assert result[-1] == DialogueTurn("nuovo", "finale")
    assert result[0].user.startswith("3")
    assert alduino_chat.decode_history(alduino_chat.encode_history(result)) == result
    assert alduino_chat.decode_history("not json") == ()


def test_corrupt_history_shapes_are_ignored_defensively():
    assert alduino_chat.decode_history('{"not": "a list"}') == ()
    assert alduino_chat.decode_history('[42,{"user":"ok","alduino":"bene"}]') == (
        DialogueTurn("ok", "bene"),
    )


def test_gemini_parser_skips_irrelevant_steps_but_requires_an_id():
    with pytest.raises(AlduinoAIError, match="missing interaction id"):
        alduino_chat._parse_gemini({
            "status": "completed",
            "steps": [42, {"type": "tool", "content": []}],
        })


def test_model_input_labels_untrusted_sections_and_clips_them():
    rendered = alduino_chat.render_model_input(
        "u" * 3000,
        history=(DialogueTurn("prima", "dopo"),),
        live_context="live",
        group_context="Mario: contesto",
        quoted_bot_text="q" * 1000,
    )

    assert "<<<DATI LIVE DEL BOT>>>" in rendered
    assert "<<<CONVERSAZIONE RECENTE>>>" in rendered
    assert "<<<CONVERSAZIONE RECENTE DEL GRUPPO>>>" in rendered
    assert "UTENTE: prima" in rendered and "ALDUINO: dopo" in rendered
    assert rendered.count("u") == alduino_chat._MAX_CURRENT_CHARS
    assert rendered.count("q") == alduino_chat._MAX_QUOTED_CHARS


def test_public_events_are_compact_and_dated():
    events = [
        PublicEvent("quiz", 1, "Quiz", "Gioca ora", "🧠"),
        PublicEvent(
            "bet", 2, "Scommessa", "Arriva presto", "🎲",
            starts_at=datetime(2026, 8, 20, 18, 30),
        ),
    ]
    rendered = alduino_chat.render_public_events(events)

    assert "Quiz (aperto adesso)" in rendered
    assert "Scommessa (programmato" in rendered
    assert alduino_chat.render_public_events([]) == (
        "Nessun evento pubblico aperto o programmato."
    )


def test_reply_has_a_hard_character_cap():
    assert len(alduino_chat._clip_reply("x" * 900)) == alduino_chat._MAX_REPLY_CHARS
