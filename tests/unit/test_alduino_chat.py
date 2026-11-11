from __future__ import annotations

from datetime import datetime

import pytest

from services import ai_service, alduino_chat
from services.alduino_chat import AlduinoAIError, DialogueTurn, GeneratedReply
from services.public_event import PublicEvent


async def test_openrouter_chat_uses_zdr_deepseek_route_and_group_context(monkeypatch):
    captured = {}

    async def openrouter(system, user, **kwargs):
        captured.update(system=system, user=user, **kwargs)
        return "sono nel discorso"

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


async def test_openrouter_failure_can_be_fail_closed(monkeypatch):
    async def down(*args, **kwargs):
        raise ai_service.AIServiceError("down")

    monkeypatch.setattr(ai_service, "generate_openrouter_completion", down)
    with pytest.raises(AlduinoAIError, match="chat providers unavailable"):
        await alduino_chat.generate_reply(system_prompt="s", current="u")


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


def test_chat_cache_prefix_preserves_all_data_and_puts_variable_fields_last():
    history = (DialogueTurn("storia utente", "storia Alduino"),)
    kwargs = {"history": history, "group_context": "Mario: dato ambientale"}
    first = alduino_chat.render_model_input("adesso uno", live_context="evento uno", quoted_bot_text="citato uno", **kwargs)
    second = alduino_chat.render_model_input("adesso due", live_context="evento due", quoted_bot_text="citato due", **kwargs)
    stable_prefix = first.split("<<<DATI LIVE DEL BOT>>>")[0]
    assert second.startswith(stable_prefix)
    assert "storia utente" in stable_prefix and "storia Alduino" in stable_prefix
    assert "Mario: dato ambientale" in stable_prefix
    assert first.index("<<<CONVERSAZIONE RECENTE>>>") < first.index("<<<CONVERSAZIONE RECENTE DEL GRUPPO>>>")
    assert first.index("evento uno") < first.index("citato uno") < first.index("adesso uno")
    assert first.endswith("<<<FINE MESSAGGIO ATTUALE>>>")


def test_reply_has_a_hard_character_cap():
    assert len(alduino_chat._clip_reply("x" * 900)) == alduino_chat._MAX_REPLY_CHARS



@pytest.mark.parametrize("prior_provider", ["gemini", "groq", "openrouter"])
async def test_migration_preserves_local_branches_without_server_state(monkeypatch, prior_provider):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    parent = SimpleNamespace(
        provider=prior_provider, provider_interaction_id="legacy-id",
        history_json=alduino_chat.encode_history((DialogueTurn("prima", "risposta"),)),
    )
    paid = AsyncMock(return_value="ricordo")
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    result = await alduino_chat.generate_reply(
        system_prompt="s", current="dopo", parent=parent,
        group_context="ambientale", live_context="evento", quoted_bot_text="citato",
    )
    assert result == GeneratedReply("ricordo", "openrouter")
    prompt = paid.call_args.args[1]
    for value in ("prima", "risposta", "ambientale", "evento", "citato", "dopo"):
        assert value in prompt
    assert "legacy-id" not in prompt


def test_public_event_context_includes_local_closing_time(monkeypatch):
    monkeypatch.setattr(alduino_chat.settings, "scheduler_timezone", "Europe/Rome")
    event = PublicEvent(
        "quiz", 25, "Quiz attivo", "5 domande", "🧠",
        ends_at=datetime(2026, 10, 28, 19, 30),
    )
    context = alduino_chat.render_public_events([event])
    assert "Quiz attivo (aperto adesso)" in context
    assert "chiusura programmata 28/10/2026 alle 20:30" in context


async def test_empty_reply_is_rejected_without_free_fallback(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(ai_service, "generate_openrouter_completion", AsyncMock(return_value=" "))
    with pytest.raises(AlduinoAIError):
        await alduino_chat.generate_reply(system_prompt="s", current="u")
