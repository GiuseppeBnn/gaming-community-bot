"""Provider boundary and bounded, branch-aware memory for Alduino chat.

OpenRouter is the only conversational provider. Telegram and SQL
orchestration stay outside provider adapters so no network call ever holds a
database transaction open.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import AlduinoTurn
from services import ai_service, schedule_service
from services.ai_routing import ProviderAttempt, ProviderUnavailable, text_router
from services.public_event import PublicEvent

_MAX_REPLY_CHARS = 500
_MAX_CURRENT_CHARS = 1500
_MAX_QUOTED_CHARS = 400


class AlduinoAIError(ProviderUnavailable):
    """No configured conversational provider could produce a usable answer."""


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
        ending = (
            " · chiusura programmata "
            + schedule_service.to_local(event.ends_at).strftime("%d/%m/%Y alle %H:%M")
            if event.ends_at is not None else ""
        )
        lines.append(
            f"- {clip_text(event.title, 120)} ({timing}): "
            f"{clip_text(event.summary, 180)}{ending}"
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
    """Build a labelled input, stable prefixes first and the current message last."""
    sections: list[str] = []
    if include_history and history:
        transcript: list[str] = []
        for turn in history:
            transcript.extend((f"UTENTE: {turn.user}", f"ALDUINO: {turn.alduino}"))
        sections.append(
            "<<<CONVERSAZIONE RECENTE>>>\n"
            + "\n".join(transcript)
            + "\n<<<FINE CONVERSAZIONE>>>"
        )
    # A reply branch grows by appending pairs; keep that reusable prefix ahead
    # of the sliding ambient window and frequently changing event state. Every
    # section and every existing privacy/size limit is preserved.
    if group_context:
        sections.append(
            "<<<CONVERSAZIONE RECENTE DEL GRUPPO>>>\n"
            f"{group_context}\n"
            "<<<FINE CONVERSAZIONE DEL GRUPPO>>>"
        )
    if live_context:
        sections.append(f"<<<DATI LIVE DEL BOT>>>\n{live_context}\n<<<FINE DATI LIVE>>>")
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
        raise AlduinoAIError("OpenRouter unavailable", status=exc.status) from exc
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
    """Continue local reply memory through OpenRouter only, including old branches."""
    history = decode_history(parent.history_json) if parent is not None else ()

    async def paid() -> GeneratedReply:
        return await _openrouter_reply(
            system_prompt=system_prompt, current=current, history=history,
            live_context=live_context, group_context=group_context,
            quoted_bot_text=quoted_bot_text,
        )

    try:
        return await text_router.generate(
            [ProviderAttempt(
                "openrouter", settings.openrouter_chat_models, paid,
                settings.openrouter_timeout_seconds,
            )],
            workload="alduino_chat",
            deadline_seconds=settings.alduino_provider_deadline_seconds,
        )
    except ProviderUnavailable as exc:
        raise AlduinoAIError("chat providers unavailable") from exc
