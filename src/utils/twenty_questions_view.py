"""Pure, HTML-safe presenters for Alduino's collaborative secret game."""

from __future__ import annotations

from datetime import datetime

from services.ai_game_types import (
    FinishReason,
    GameView,
    PersonalQuota,
    QuestionStartResult,
    QuestionVerdict,
    TerminalResult,
    TurnOutcome,
    TurnRejectReason,
    TurnResult,
    TurnView,
    TwentyQuestionsPolicy,
)
from utils.daytime import to_local
from utils.text import esc

_CARD_TURN_INPUT_CHARS = 96
GAME_INSTRUCTIONS = (
    "Per partecipare continua a rispondere sempre e solo a questo messaggio facendo una domanda!\n"
    "Se vuoi provare ad indovinare la risposta digita\n"
    "<code>Risposta: titolo del gioco</code>"
)
GAME_STATUS_HINT = "Digita /gioco_alduino per avere le informazioni aggiornate"
GAME_REWARD_HINT = (
    "Riceverete i CoInn solo se qualcuno indovina il gioco. "
    "Il premio mostrato è una stima: l'importo definitivo sarà calcolato a fine partita."
)


def _reward_warning(policy: TwentyQuestionsPolicy) -> str:
    if policy.max_coins_per_participant > 0 and (
        policy.question_penalty_bps > 0 or policy.wrong_guess_penalty_bps > 0
    ):
        return "Delle domande o dei tentativi errati faranno diminuire il tuo premio in CoInn."
    return ""


def render_expiry(expires_at: datetime) -> str:
    """Present stored naive UTC in the community timezone, including DST."""
    return f"🗓️ Scade: <b>{to_local(expires_at).strftime('%d/%m/%Y %H:%M %Z')}</b>"


def _quota_line(quota: PersonalQuota) -> str:
    return (
        "📌 <b>Per te</b>: "
        f"{quota.questions_left} domande · {quota.guesses_left} tentativi rimasti."
    )


def _verdict_label(verdict: QuestionVerdict | None) -> str:
    if verdict is None:
        return "VERDETTO NON DISPONIBILE"
    return {
        QuestionVerdict.si: "SÌ",
        QuestionVerdict.no: "NO",
        QuestionVerdict.forse: "NON LO SO",
        QuestionVerdict.non_lo_so: "NON LO SO",
        QuestionVerdict.usa_risposta: "PROVA A INDOVINARE",
    }.get(verdict, "VERDETTO NON DISPONIBILE")


def _turn_line(turn: TurnView) -> str:
    visible_input = turn.input_text
    if len(visible_input) > _CARD_TURN_INPUT_CHARS:
        visible_input = f"{visible_input[:_CARD_TURN_INPUT_CHARS - 1]}…"
    if turn.kind.value == "question":
        icons = {
            QuestionVerdict.si: "✅",
            QuestionVerdict.no: "❌",
            QuestionVerdict.forse: "🤔",
            QuestionVerdict.non_lo_so: "🤔",
            QuestionVerdict.usa_risposta: "🎯",
        }
        icon = icons.get(turn.verdict, "➖") if turn.verdict is not None else "➖"
        return f"{icon} {esc(visible_input)}"
    icon = "🏆" if turn.correct else "💥"
    return f"{icon} Tentativo: {esc(visible_input)}"


def _expiry_lines(expires_at: datetime | None) -> list[str]:
    # Absolute expiry is useful; a static countdown becomes stale immediately.
    if expires_at is None:
        return []
    return [render_expiry(expires_at)]


def render_policy(policy: TwentyQuestionsPolicy) -> str:
    """Render only the immutable policy snapshot, never a live ORM row."""
    return "\n".join((
        "📜 <b>Regole del gioco segreto di Alduino</b>",
        (
            "Ogni persona ha "
            f"<b>{policy.questions_per_user} domande</b> · "
            f"<b>{policy.guesses_per_user} tentativi</b>."
        ),
        (
            f"Premio massimo: <b>{policy.max_coins_per_participant} CoInn</b> "
            "per partecipante."
        ),
        _reward_warning(policy),
        f"✨ Ogni partecipante valido riceve <b>{policy.xp_per_participant} XP</b> alla chiusura.",
    ))


def render_public_help(policy: TwentyQuestionsPolicy) -> str:
    """Render reusable public instructions from the same persisted policy."""
    return "\n\n".join((
        "🐲 <b>Il gioco segreto di Alduino</b>",
        "Indovina il videogioco segreto facendo domande: "
        "Alduino risponde solo <b>SÌ</b> o <b>NO</b>. "
        "Se mancano elementi, <b>NON LO SO</b>: domanda e premio restano intatti.",
        GAME_INSTRUCTIONS,
        render_policy(policy),
        GAME_REWARD_HINT,
        "Duplicati, problemi tecnici e titoli senza <code>RISPOSTA:</code> non consumano nulla.",
        GAME_STATUS_HINT,
    ))


def render_live_card(
    view: GameView,
    *,
    now: datetime,
    open_preview: bool = False,
    personal_quota: PersonalQuota | None = None,
    in_group: bool = True,
) -> str:
    """Render the shared, non-secret card from a small immutable game view."""
    policy = view.policy
    projection = view.projection
    lines = [
        "🐲 <b>Il gioco segreto di Alduino</b>",
        f"<b>{esc(view.title)}</b>",
        "",
        *_expiry_lines(view.expires_at),
        "",
        (
            "👥 Partecipanti: "
            f"<b>{view.participant_count}</b> · Domande: <b>{view.question_count}</b> · "
            f"Tentativi errati: <b>{view.wrong_guess_count}</b>"
        ),
        (
            f"🎲 Regola: <b>{policy.questions_per_user} domande · "
            f"{policy.guesses_per_user} tentativi</b> per persona."
        ),
        "Alduino risponde solo <b>SÌ</b> o <b>NO</b>. "
        "<b>NON LO SO</b> non consuma la domanda.",
        (
            f"💰 Quota stimata: <b>{projection.share} CoInn</b> a persona. "
            f"Massimo <b>{policy.max_coins_per_participant}</b>."
        ),
        _reward_warning(policy),
        GAME_REWARD_HINT,
        f"✨ Partecipare vale <b>{policy.xp_per_participant} XP</b> alla chiusura.",
    ]
    recent = view.recent_turns[-6:]
    if recent:
        lines.extend(("", "<b>Ultimi turni validi</b>"))
        lines.extend(_turn_line(turn) for turn in recent)
    if personal_quota is not None:
        lines.extend(("", _quota_line(personal_quota)))
    if view.status == "running" or open_preview:
        lines.extend((
            "",
            GAME_INSTRUCTIONS if in_group else (
                "La partita si gioca nel gruppo: torna lì e rispondi al messaggio del gioco "
                "con una domanda o <code>Risposta: titolo del gioco</code>."
            ),
        ))
    lines.extend(("", GAME_STATUS_HINT))
    return "\n".join(lines)


def render_terminal_card(result: TerminalResult, *, winner_html: str | None = None) -> str:
    """Render a terminal settlement without exposing raw user data or ORM rows."""
    reward = result.reward
    xp_award = result.allocations[0].xp if result.allocations else 0
    lines = [
        "🐲 <b>Il gioco segreto di Alduino — concluso</b>",
        f"<b>{esc(result.title)}</b>",
        f"🔓 Il gioco era <b>{esc(result.answer)}</b>.",
        (
            f"👥 Partecipanti: <b>{reward.participant_count}</b> · "
            f"Domande: <b>{reward.question_count}</b> · "
            f"Tentativi errati: <b>{reward.wrong_guess_count}</b>"
        ),
        f"✨ XP partecipazione: <b>{xp_award}</b>",
    ]
    if result.finish_reason is FinishReason.victory:
        lines.extend((
            f"🏆 Vincitore: {winner_html or 'un partecipante'}",
            f"💰 CoInn pagati: <b>{reward.paid_pool}</b>",
            (
                "Formula: "
                f"Base {reward.base_amount} − penalità {reward.penalty_amount} = "
                f"{reward.computed_pool}; quota {reward.share} CoInn per partecipante · "
                f"resto non distribuito: {reward.remainder} CoInn."
            ),
        ))
    else:
        if reward.settlement_status == "void":
            lines.append("⚪ Nessun partecipante valido: partita annullata.")
        elif result.finish_reason is FinishReason.expired:
            lines.append("⌛ Tempo scaduto: il gioco non è stato indovinato.")
        elif result.finish_reason is FinishReason.admin_closed:
            lines.append("🛑 Partita chiusa dallo staff: il gioco non è stato indovinato.")
        else:
            lines.append("⚪ Partita conclusa senza vincitore.")
        lines.append("💰 CoInn: 0 — gioco non indovinato")
    lines.extend(("", GAME_STATUS_HINT))
    return "\n".join(lines)


def render_question_start(result: QuestionStartResult) -> str:
    """Render the immediate typed outcome of a question claim or reuse."""
    if result.outcome is TurnOutcome.claimed:
        message = "🐲 Alduino sta ragionando sulla tua domanda…"
    elif result.outcome is TurnOutcome.reused:
        message = f"🐲 Domanda già fatta: <b>{_verdict_label(result.cached_verdict)}</b>."
    else:
        message = _reject_message(result.reason)
    return f"{message}\n\n{_quota_line(result.quota)}"


def render_personal_turn(result: TurnResult) -> str:
    """Render any typed question/guess completion without raw provider output."""
    if result.outcome is TurnOutcome.recorded:
        if result.verdict is not None:
            message = f"🐲 <b>{_verdict_label(result.verdict)}</b>"
        elif result.correct is True:
            message = "🏆 Hai indovinato il gioco!"
        elif result.correct is False:
            message = "💥 Non è lui."
        else:
            message = "🐲 Turno registrato."
    elif result.outcome is TurnOutcome.reused:
        message = f"🐲 Domanda già fatta: <b>{_verdict_label(result.verdict)}</b>."
    else:
        message = _reject_message(result.reason)
    return f"{message}\n\n{_quota_line(result.quota)}"


def _reject_message(reason: TurnRejectReason | None) -> str:
    if reason is None:
        return "🐲 Non riesco a registrare questo turno: riprova."
    return {
        TurnRejectReason.busy: "🐲 Alduino sta già valutando un'altra domanda: riprova tra poco.",
        TurnRejectReason.closed: "🐲 Questa partita non è più aperta.",
        TurnRejectReason.expired: "⌛ Il tempo è scaduto: il gioco è stato chiuso.",
        TurnRejectReason.question_quota: "🐲 Hai esaurito le tue domande valide.",
        TurnRejectReason.guess_quota: "🐲 Hai esaurito i tuoi tentativi validi.",
        TurnRejectReason.duplicate_guess: "🐲 Titolo già tentato: non consuma nulla.",
        TurnRejectReason.invalid_input: "🐲 Tienila tra 1 e 500 caratteri, avventuriero.",
        TurnRejectReason.hash_collision: "🐲 Ho rilevato un input incoerente: riprova senza modificarlo.",
        TurnRejectReason.providers_unavailable: (
            "🔥 Alduino ha il cervello in fumo. La domanda non è stata consumata: riprova."
        ),
        TurnRejectReason.lost_claim: "🐲 Il turno non è più disponibile: riprova.",
        TurnRejectReason.answer_confirmation_required: (
            "🐲 Per tentare il titolo reinvia <code>RISPOSTA: titolo del gioco</code>."
        ),
        TurnRejectReason.insufficient_information: (
            "🐲 <b>NON LO SO</b> — domanda non consumata."
        ),
    }.get(reason, "🐲 Non riesco a registrare questo turno: riprova.")


def render_personal_status(
    view: GameView, quota: PersonalQuota, *, now: datetime, in_group: bool = True,
) -> str:
    """Combine the public projection with the caller's own remaining quota."""
    return render_live_card(view, now=now, personal_quota=quota, in_group=in_group)
