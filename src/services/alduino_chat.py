"""Provider boundary and bounded, branch-aware memory for Alduino chat.

OpenRouter, Gemini and Groq are swappable conversational lanes. Telegram and SQL
orchestration stay outside provider adapters so no network call ever holds a
database transaction open.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any, Iterable, Sequence

import aiohttp
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import AlduinoTurn
from services import ai_service, schedule_service
from services.public_event import PublicEvent

log = logging.getLogger(__name__)

GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_API_REVISION = "2026-05-20"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_STALE_PARENT_STATUSES = frozenset({400, 404})
_MAX_REPLY_CHARS = 500
_MAX_CURRENT_CHARS = 1500
_MAX_QUOTED_CHARS = 400
_RETRY_DELAY_SECONDS = 0.5


class AlduinoAIError(RuntimeError):
    """No configured conversational provider could produce a usable answer."""


class _GeminiHTTPError(AlduinoAIError):
    def __init__(self, status: int):
        super().__init__(f"status {status}")
        self.status = status


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    user: str
    alduino: str


@dataclass(frozen=True, slots=True)
class GeneratedReply:
    text: str
    provider: str
    interaction_id: str | None = None


def clip_text(text: str, limit: int = _MAX_CURRENT_CHARS) -> str:
    return (text or "").strip()[:limit]


def _clip_reply(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise AlduinoAIError("empty completion")
    if len(value) <= _MAX_REPLY_CHARS:
        return value
    return value[: _MAX_REPLY_CHARS - 1].rstrip() + "…"


def decode_history(raw: str) -> tuple[DialogueTurn, ...]:
    """Read a stored snapshot defensively; corrupt memory must never break chat."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    turns: list[DialogueTurn] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        user, alduino = item.get("user"), item.get("alduino")
        if isinstance(user, str) and isinstance(alduino, str):
            turns.append(DialogueTurn(user=user, alduino=alduino))
    return tuple(turns)


def bounded_history(
    previous: Sequence[DialogueTurn], current: DialogueTurn,
    *, max_turns: int | None = None, max_chars: int | None = None,
) -> tuple[DialogueTurn, ...]:
    """Append a pair while retaining the newest useful context under both caps."""
    turn_cap = max_turns or settings.alduino_history_turns
    char_cap = max_chars or settings.alduino_history_chars
    kept = list(previous) + [current]
    while len(kept) > 1 and (
        len(kept) > turn_cap
        or sum(len(turn.user) + len(turn.alduino) for turn in kept) > char_cap
    ):
        kept.pop(0)
    return tuple(kept)


def encode_history(turns: Sequence[DialogueTurn]) -> str:
    return json.dumps(
        [{"user": turn.user, "alduino": turn.alduino} for turn in turns],
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def find_parent(
    session: AsyncSession, group_id: int, bot_message_id: int | None,
) -> AlduinoTurn | None:
    if bot_message_id is None:
        return None
    return (await session.execute(select(AlduinoTurn).where(
        AlduinoTurn.group_id == group_id,
        AlduinoTurn.bot_message_id == bot_message_id,
    ))).scalar_one_or_none()


async def record_turn(
    session: AsyncSession,
    *,
    group_id: int,
    user_tg_id: int,
    user_message_id: int,
    bot_message_id: int,
    parent: AlduinoTurn | None,
    user_text: str,
    reply: GeneratedReply,
) -> AlduinoTurn:
    """Persist one completed turn and prune old rows without losing its snapshot."""
    prior = decode_history(parent.history_json) if parent is not None else ()
    history = bounded_history(prior, DialogueTurn(user=user_text, alduino=reply.text))
    row = AlduinoTurn(
        group_id=group_id,
        user_tg_id=user_tg_id,
        user_message_id=user_message_id,
        bot_message_id=bot_message_id,
        parent_turn_id=parent.id if parent is not None else None,
        input_text=clip_text(user_text),
        output_text=reply.text[:1000],
        history_json=encode_history(history),
        provider=reply.provider,
        provider_interaction_id=reply.interaction_id,
    )
    session.add(row)
    await session.flush()

    # Bounded storage per group. Context snapshots make deletion safe even if a
    # retained child once descended from a now-pruned root.
    stale_ids = (
        select(AlduinoTurn.id)
        .where(AlduinoTurn.group_id == group_id)
        .order_by(AlduinoTurn.id.desc())
        .offset(settings.alduino_memory_rows_per_group)
    )
    await session.execute(delete(AlduinoTurn).where(AlduinoTurn.id.in_(stale_ids)))
    return row


def render_public_events(events: Iterable[PublicEvent]) -> str:
    """Compact live reference; event text remains untrusted model input."""
    lines: list[str] = []
    for event in events:
        if event.is_open:
            timing = "aperto adesso"
        else:
            assert event.starts_at is not None
            timing = "programmato " + schedule_service.to_local(event.starts_at).strftime(
                "%d/%m/%Y alle %H:%M"
            )
        lines.append(
            f"- {clip_text(event.title, 120)} ({timing}): "
            f"{clip_text(event.summary, 180)}"
        )
    return "\n".join(lines) if lines else "Nessun evento pubblico aperto o programmato."


def render_model_input(
    current: str,
    *,
    history: Sequence[DialogueTurn] = (),
    live_context: str = "",
    group_context: str = "",
    quoted_bot_text: str = "",
    include_history: bool = True,
) -> str:
    """Build one explicitly-labelled, injection-resistant conversational input."""
    sections: list[str] = []
    if live_context:
        sections.append(f"<<<DATI LIVE DEL BOT>>>\n{live_context}\n<<<FINE DATI LIVE>>>")
    if group_context:
        sections.append(
            "<<<CONVERSAZIONE RECENTE DEL GRUPPO>>>\n"
            f"{group_context}\n"
            "<<<FINE CONVERSAZIONE DEL GRUPPO>>>"
        )
    if include_history and history:
        transcript: list[str] = []
        for turn in history:
            transcript.extend((f"UTENTE: {turn.user}", f"ALDUINO: {turn.alduino}"))
        sections.append(
            "<<<CONVERSAZIONE RECENTE>>>\n"
            + "\n".join(transcript)
            + "\n<<<FINE CONVERSAZIONE>>>"
        )
    if quoted_bot_text:
        sections.append(
            "<<<MESSAGGIO DEL BOT A CUI RISPONDE>>>\n"
            f"{clip_text(quoted_bot_text, _MAX_QUOTED_CHARS)}\n"
            "<<<FINE MESSAGGIO DEL BOT>>>"
        )
    sections.append(
        "<<<MESSAGGIO ATTUALE DELL'UTENTE>>>\n"
        f"{clip_text(current)}\n"
        "<<<FINE MESSAGGIO ATTUALE>>>"
    )
    return "\n\n".join(sections)


async def _gemini_post(payload: dict[str, Any]) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=settings.alduino_timeout_seconds)
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    GEMINI_INTERACTIONS_URL,
                    headers={
                        "x-goog-api-key": settings.gemini_api_key,
                        "Api-Revision": _API_REVISION,
                    },
                    json=payload,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if not isinstance(data, dict):
                            raise AlduinoAIError("malformed response")
                        return data
                    log.warning("Gemini Alduino ha risposto con status %s.", response.status)
                    if response.status not in _RETRYABLE or attempt == 2:
                        raise _GeminiHTTPError(response.status)
        except asyncio.TimeoutError as exc:
            if attempt == 2:
                raise AlduinoAIError("timeout") from exc
        except aiohttp.ClientError as exc:
            if attempt == 2:
                raise AlduinoAIError("network error") from exc
        await asyncio.sleep(_RETRY_DELAY_SECONDS)
    raise AlduinoAIError("unreachable")  # pragma: no cover - loop always returns/raises


def _parse_gemini(data: dict[str, Any]) -> GeneratedReply:
    if data.get("status") != "completed":
        raise AlduinoAIError(f"interaction {data.get('status', 'malformed')}")
    pieces: list[str] = []
    for step in data.get("steps", []):
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
    interaction_id = data.get("id")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise AlduinoAIError("missing interaction id")
    return GeneratedReply(
        text=_clip_reply("".join(pieces)),
        provider="gemini",
        interaction_id=interaction_id[:512],
    )


async def _gemini_reply(
    *,
    system_prompt: str,
    current: str,
    history: Sequence[DialogueTurn],
    live_context: str,
    group_context: str,
    quoted_bot_text: str,
    previous_interaction_id: str | None,
) -> GeneratedReply:
    if not settings.gemini_api_key:
        raise AlduinoAIError("missing Gemini api key")

    def payload(*, stateful: bool) -> dict[str, Any]:
        value: dict[str, Any] = {
            "model": settings.alduino_gemini_model,
            "input": render_model_input(
                current,
                history=history,
                live_context=live_context,
                group_context=group_context,
                quoted_bot_text=quoted_bot_text,
                include_history=not stateful,
            ),
            "system_instruction": system_prompt,
            "store": True,
            "generation_config": {
                "max_output_tokens": 280,
                "thinking_level": settings.alduino_thinking_level,
                "thinking_summaries": "none",
            },
        }
        if stateful:
            value["previous_interaction_id"] = previous_interaction_id
        return value

    if previous_interaction_id:
        try:
            return _parse_gemini(await _gemini_post(payload(stateful=True)))
        except _GeminiHTTPError as exc:
            if exc.status not in _STALE_PARENT_STATUSES:
                raise
            # Free-tier server state expires after one day. The local bounded
            # branch is authoritative, so expiration degrades to stateless chat.
            log.info("Stato Gemini scaduto/non valido: ricostruisco il ramo locale.")
    return _parse_gemini(await _gemini_post(payload(stateful=False)))


async def _groq_reply(
    *, system_prompt: str, current: str, history: Sequence[DialogueTurn],
    live_context: str, quoted_bot_text: str,
    group_context: str = "",
) -> GeneratedReply:
    prompt = render_model_input(
        current,
        history=history,
        live_context=live_context,
        group_context=group_context,
        quoted_bot_text=quoted_bot_text,
    )
    try:
        text = await ai_service.generate_groq_completion(
            system_prompt, prompt, max_tokens=280,
        )
    except ai_service.AIServiceError as exc:
        raise AlduinoAIError("Groq unavailable") from exc
    return GeneratedReply(text=_clip_reply(text), provider="groq")


async def _openrouter_reply(
    *,
    system_prompt: str,
    current: str,
    history: Sequence[DialogueTurn],
    live_context: str,
    quoted_bot_text: str,
    group_context: str,
) -> GeneratedReply:
    prompt = render_model_input(
        current,
        history=history,
        live_context=live_context,
        group_context=group_context,
        quoted_bot_text=quoted_bot_text,
    )
    try:
        text = await ai_service.generate_openrouter_completion(
            system_prompt,
            prompt,
            max_tokens=280,
            feature="alduino_chat",
            models=ai_service.parse_model_list(settings.openrouter_chat_models),
            require_zdr=True,
        )
    except ai_service.AIServiceError as exc:
        raise AlduinoAIError("OpenRouter unavailable") from exc
    return GeneratedReply(text=_clip_reply(text), provider="openrouter")


async def generate_reply(
    *,
    system_prompt: str,
    current: str,
    parent: AlduinoTurn | None = None,
    live_context: str = "",
    group_context: str = "",
    quoted_bot_text: str = "",
) -> GeneratedReply:
    """Generate through the selected provider, with a narrow Groq failover."""
    history = decode_history(parent.history_json) if parent is not None else ()
    previous_id = (
        parent.provider_interaction_id
        if parent is not None and parent.provider == "gemini"
        else None
    )
    if settings.alduino_provider == "groq":
        return await _groq_reply(
            system_prompt=system_prompt, current=current, history=history,
            live_context=live_context, group_context="",
            quoted_bot_text=quoted_bot_text,
        )
    if settings.alduino_provider == "openrouter":
        try:
            return await _openrouter_reply(
                system_prompt=system_prompt,
                current=current,
                history=history,
                live_context=live_context,
                group_context=group_context,
                quoted_bot_text=quoted_bot_text,
            )
        except AlduinoAIError as exc:
            if not settings.alduino_fallback_to_groq:
                raise
            log.warning("OpenRouter Alduino non disponibile (%s): uso Groq.", exc)
            return await _groq_reply(
                system_prompt=system_prompt,
                current=current,
                history=history,
                live_context=live_context,
                group_context="",
                quoted_bot_text=quoted_bot_text,
            )
    try:
        return await _gemini_reply(
            system_prompt=system_prompt,
            current=current,
            history=history,
            live_context=live_context,
            group_context="",
            quoted_bot_text=quoted_bot_text,
            previous_interaction_id=previous_id,
        )
    except AlduinoAIError as exc:
        if not settings.alduino_fallback_to_groq:
            raise
        log.warning("Gemini Alduino non disponibile (%s): uso Groq.", exc)
        return await _groq_reply(
            system_prompt=system_prompt, current=current, history=history,
            live_context=live_context, group_context="",
            quoted_bot_text=quoted_bot_text,
        )
