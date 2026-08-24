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
from utils.text import esc, format_duration

_CARD_TURN_INPUT_CHARS = 96


def _percentage(basis_points: int) -> str:
    whole, fraction = divmod(basis_points, 100)
    if fraction == 0:
        return f"{whole}%"
    return f"{whole},{f'{fraction:02d}'.rstrip('0')}%"


def _minimum_share(view: GameView) -> int | None:
    participants = view.projection.participant_count
    if participants <= 0:
        return None
    minimum_pool = (
        view.projection.base_amount * view.policy.minimum_bps + 9_999
    ) // 10_000
    return minimum_pool // participants


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
        QuestionVerdict.forse: "FORSE",
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
            QuestionVerdict.usa_risposta: "🎯",
        }
        icon = icons.get(turn.verdict, "➖") if turn.verdict is not None else "➖"
        return f"{icon} {esc(visible_input)}"
    icon = "🏆" if turn.correct else "💥"
    return f"{icon} Tentativo: {esc(visible_input)}"


def _expiry_lines(expires_at: datetime | None, now: datetime) -> list[str]:
    if expires_at is None:
        return ["🕰️ Scadenza: <b>non disponibile</b>"]
    seconds_left = int((expires_at - now).total_seconds())
    residual = "scaduto" if seconds_left <= 0 else format_duration(seconds_left)
    return [
        f"🕰️ Scade: <b>{expires_at.strftime('%d/%m/%Y %H:%M UTC')}</b>",
        f"⏳ Tempo residuo: <b>{residual}</b>",
    ]


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
            "per partecipante; il pool non scende sotto "
            f"il <b>{_percentage(policy.minimum_bps)}</b> del massimo."
        ),
        (
            "Penalità sul pool: "
            f"<b>{_percentage(policy.question_penalty_bps)}</b> per domanda valida · "
            f"<b>{_percentage(policy.wrong_guess_penalty_bps)}</b> per tentativo errato."
        ),
        f"✨ Ogni partecipante valido riceve <b>{policy.xp_per_participant} XP</b> alla chiusura.",
    ))


def render_public_help(policy: TwentyQuestionsPolicy) -> str:
    """Render reusable public instructions from the same persisted policy."""
    return "\n\n".join((
        "🐲 <b>Il gioco segreto di Alduino</b>",
        (
            "Rispondi alla card nel gruppo con una domanda. Per provare il titolo usa "
            "<code>RISPOSTA: titolo del gioco</code>."
        ),
        render_policy(policy),
        (
            "Duplicati, problemi tecnici e titoli scritti senza <code>RISPOSTA:</code> "
            "non consumano nulla. La quota mostrata è una proiezione: può crescere "
            "con nuovi partecipanti e non è un saldo maturato."
        ),
    ))


def render_live_card(
    view: GameView,
    *,
    now: datetime,
    open_preview: bool = False,
) -> str:
    """Render the shared, non-secret card from a small immutable game view."""
    policy = view.policy
    projection = view.projection
    minimum_share = _minimum_share(view)
    minimum_text = (
        f"<b>{minimum_share} CoInn</b> per partecipante"
        if minimum_share is not None else "<b>—</b> senza partecipanti"
    )
    lines = [
        "🐲 <b>Il gioco segreto di Alduino</b>",
        f"<b>{esc(view.title)}</b>",
        "",
        *_expiry_lines(view.expires_at, now),
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
        (
            f"🎁 Massimo: <b>{policy.max_coins_per_participant} CoInn</b> per partecipante · "
            f"minimo attuale: {minimum_text} "
            f"({_percentage(policy.minimum_bps)} del pool base)."
        ),
        (
            "📉 Penalità: "
            f"{_percentage(policy.question_penalty_bps)} per domanda valida · "
            f"{_percentage(policy.wrong_guess_penalty_bps)} per tentativo errato."
        ),
        (
            f"💰 Quota stimata: <b>{projection.share} CoInn</b> a partecipante "
            f"(pool previsto {projection.computed_pool} CoInn · resto {projection.remainder} CoInn)."
        ),
        (
            "La quota è una <b>proiezione se si vincesse ora</b>: può crescere con "
            "nuovi partecipanti e non è un saldo maturato."
        ),
        f"✨ Partecipare vale <b>{policy.xp_per_participant} XP</b> alla chiusura.",
    ]
    recent = view.recent_turns[-6:]
    if recent:
        lines.extend(("", "<b>Ultimi turni validi</b>"))
        lines.extend(_turn_line(turn) for turn in recent)
    if view.status == "running" or open_preview:
        lines.extend((
            "",
            "Rispondi <b>a questo messaggio</b> con una domanda.",
            "Per tentare: <code>RISPOSTA: titolo del gioco</code>",
        ))
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
    }.get(reason, "🐲 Non riesco a registrare questo turno: riprova.")


def render_personal_status(view: GameView, quota: PersonalQuota, *, now: datetime) -> str:
    """Combine the public projection with the caller's own remaining quota."""
    return f"{render_live_card(view, now=now)}\n\n{_quota_line(quota)}"
