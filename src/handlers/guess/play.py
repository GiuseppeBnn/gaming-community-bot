"""Playing a round in private: one medium, N attempts, a free-text answer.

There is no timer task and no in-memory state. The deadline is
``session.started_at + round.time_limit_seconds``, derived and checked at every
submission, so it survives a restart and re-entering cannot reset it. The quiz
needs asyncio timers because its clock is per question; here it is per session,
and the simpler shape is the safer one. The player is told the deadline as a wall
clock time up front, so nobody waits for a "time's up!" that no timer will send.

**The guard order below is load-bearing**: command → cooldown → already solved →
deadline → attempts → judge. A command (``/start`` from re-tapping «avvia»), a
throttled, late or budget-exhausted message must never cost an attempt and must
never reach the model.

A wrong answer never echoes the correct one, and the model's own words never
reach the player — only the boolean it produced.
"""

from __future__ import annotations

from aiogram import F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from services import guess_judge, guess_service, schedule_service
from utils import cooldown
from utils.text import esc

from handlers.callbacks import GuessPlayCb
from handlers.guess._shared import kind_of, log, router, send_media

_ANSWER_BUCKET = "guess_answer"

#: Telegram clears a chat action after ~5s, and aiogram's default refresh is
#: exactly 5.0 — the two racing produce a visible flicker on a slow verdict.
#: Refreshing just inside the window keeps "sto scrivendo…" continuous.
_TYPING_REFRESH = 4.0


class GuessPlayStates(StatesGroup):
    answering = State()


def _playing_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚪 Esci dal gioco", callback_data=GuessPlayCb(action="quit").pack())
    return b.as_markup()


async def _reply(
    message: Message,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> None:
    """Answer the player, leaving only the newest message holding buttons.

    Five wrong answers used to leave five live «🚪 Esci» buttons up the chat, all
    of them working and none of them obviously current. Stripping the superseded
    one keeps exactly one live control on screen.

    The cleanup is best-effort and deliberately runs *before* the new message: it
    is cosmetic, while the text below it is the verdict. An edit refused because
    the message is too old must never turn a correct answer into an error.
    """
    data = await state.get_data()
    previous = data.get("kb_message_id")
    if previous and kb is not None:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=previous, reply_markup=None
            )
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.debug("Tastiera %s non rimovibile: %s", previous, exc)

    sent = await message.answer(text, reply_markup=kb)
    if kb is not None:
        await state.update_data(kb_message_id=sent.message_id)


async def _end(message: Message, state: FSMContext, text: str) -> None:
    """Close the game out: drop the last live keyboard, clear state, then answer.

    The terminal replies (solved, out of attempts, expired, closed) used to
    `state.clear()` and `message.answer` **without** stripping the previous
    «🚪 Esci» button — leaving a live control on a game that was already over.
    Pressing it then matched no handler (the state was gone) and the button spun
    until Telegram gave up: the "si blocca dopo alcuni tentativi" report. Stripping
    it here removes the button before the state that answers it disappears.

    Best-effort, like `_reply`: a refused edit (message too old) must never turn a
    correct answer into an error.
    """
    data = await state.get_data()
    previous = data.get("kb_message_id")
    if previous:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=previous, reply_markup=None
            )
        except Exception as exc:  # noqa: BLE001 — cosmetic, see the docstring
            log.debug("Tastiera %s non rimovibile: %s", previous, exc)
    await state.clear()
    await message.answer(text)


def _status_line(round_, sess, left: int) -> str:
    """Where the player stands, on every message that expects an answer.

    Both numbers used to be invisible: the attempts showed only on a wrong
    answer, the deadline only once on the way in. They are the two facts that
    decide how you play, so they go on every screen that asks you to play.

    The deadline is a wall clock and not a countdown because nothing will ever
    send a "time's up!" — the check is stateless and happens on submission, so an
    absolute time is the only honest way to state it.
    """
    parts = [f"🎯 <b>{left}</b> tentativi rimasti su {round_.max_attempts}"]
    deadline = guess_service.deadline(round_, sess)
    if deadline is not None:
        parts.append(f"⏱️ fino alle <b>{schedule_service.to_local(deadline):%H:%M}</b>")
    return " · ".join(parts)


async def start_guess_session(
    message: Message,
    db_session: AsyncSession,
    state: FSMContext,
    round_id: int,
    *,
    user_id: int | None = None,
) -> None:
    """Deep-link ``<kind>_<id>``: start (or resume) playing this round in private.

    `user_id` is passed explicitly by callers whose `message` belongs to the bot
    rather than to the player — the «Riprendi» button, whose `callback.message`
    the bot itself sent. The same reason `start_guess_creation` takes
    `creator_id`. Reaching into `message.from_user` to fix it up is not an
    option: aiogram's models are frozen pydantic instances and assigning to one
    raises at runtime.
    """
    round_ = await guess_service.get_round(db_session, round_id)
    if round_ is None:
        await message.answer("⚠️ Round non trovato.")
        return
    if round_.status == "finished":
        await message.answer("🏁 Questo round è già terminato.")
        return
    if round_.status != "running":
        await message.answer(
            "⏳ Questo round non è ancora iniziato. Aspetta che un admin lo avvii."
        )
        return

    player_id = user_id if user_id is not None else message.from_user.id
    sess = await guess_service.start_or_resume(db_session, round_id, player_id)
    await db_session.commit()

    if sess.solved_at is not None:
        await message.answer(
            f"✅ Hai <b>già indovinato</b> «{esc(round_.title)}» in "
            f"<b>{sess.solved_attempts}</b> tentativi.\n"
            "Aspetta la chiusura per vedere il podio! 🏁"
        )
        return

    left = await guess_service.attempts_left(db_session, round_, player_id)
    if left <= 0:
        await message.answer("❌ Hai esaurito i tentativi per questo round.")
        return

    spec = kind_of(round_.kind)
    try:
        await send_media(message.bot, message.chat.id, round_.media_file_id, round_.media_kind)
    except Exception as exc:  # noqa: BLE001 — a dead file_id must not read as a bug
        log.warning("Media del round %s non inviabile: %s", round_id, exc)
        await message.answer(
            "⚠️ Non riesco a caricare il contenuto di questo round. Segnalalo a un admin."
        )
        return

    await state.set_state(GuessPlayStates.answering)
    await state.update_data(round_id=round_id)
    await _reply(
        message,
        state,
        f"{spec.emoji} <b>{esc(round_.title)}</b>\n\n"
        "Scrivimi il <b>titolo del gioco</b>.\n"
        f"{_status_line(round_, sess, left)}\n"
        "<i>Meno tentativi usi, più in alto finisci nel podio!</i>",
        _playing_kb(),
    )


@router.message(GuessPlayStates.answering, F.chat.type == ChatType.PRIVATE)
async def fsm_answer(message: Message, db_session: AsyncSession, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        return  # a sticker or a photo is not an attempt
    if raw.startswith("/"):
        # A command typed mid-round — typically /start, from re-tapping «avvia» on
        # the deep-link when a player hasn't realised the round already began — must
        # never cost an attempt. It is simply ignored while answering.
        return

    round_id = (await state.get_data()).get("round_id")
    round_ = await guess_service.get_round(db_session, round_id) if round_id else None
    if round_ is None or round_.status != "running":
        await _end(message, state, "🏁 Questo round è chiuso.")
        return

    # Cooldown FIRST: a throttled message must not cost an attempt.
    # exempt_admin=False on purpose — a game is a game, and an admin who can
    # hammer the judge is an admin with a better shot at the podium.
    if not await cooldown.guard(
        message,
        _ANSWER_BUCKET,
        settings.guess_answer_cooldown_seconds,
        exempt_admin=False,
        notice="⏳ Vai più piano! Riprova tra {s}s.",
    ):
        return

    user_id = message.from_user.id
    sess = await guess_service.start_or_resume(db_session, round_.id, user_id)
    if sess.solved_at is not None:
        await _end(message, state, "✅ Hai già indovinato questo round.")
        return

    deadline = guess_service.deadline(round_, sess)
    if deadline is not None and guess_service.now() > deadline:
        await _end(message, state, "⏱️ <b>Tempo scaduto</b> per questo round.")
        return

    if await guess_service.attempts_left(db_session, round_, user_id) <= 0:
        await _end(message, state, "❌ Tentativi <b>esauriti</b> per questo round.")
        return

    # Last guard before the model, and the one production needed: when the judge
    # keeps failing, stop feeding it. Un-judged answers cost no attempts, so the
    # player's budget is intact and waiting — but without a bound this would be an
    # unlimited free channel to Groq for anyone willing to type.
    if await guess_service.unverified_left(db_session, round_, user_id) <= 0:
        await _reply(
            message,
            state,
            "⚠️ Il <b>giudice non risponde</b> in questo momento.\n"
            "I tuoi tentativi sono <b>salvi</b> — riprova fra qualche minuto.",
            _playing_kb(),
        )
        return

    # "Sto scrivendo…" for as long as the judge takes, and gone by itself the
    # moment it answers. The judge is a network call that can run into seconds,
    # and silence reads as a broken bot.
    #
    # A chat action rather than a "⏳ attendi" message on purpose: Telegram clears
    # it on its own, so there is nothing to delete, nothing to leak if we raise,
    # and no extra message competing with the verdict that follows. `interval`
    # under Telegram's 5s expiry keeps it alive across a slow call.
    async with ChatActionSender.typing(
        bot=message.bot, chat_id=message.chat.id, interval=_TYPING_REFRESH
    ):
        verdict = await guess_judge.judge(db_session, round_, raw)
    outcome = await guess_service.record_attempt(db_session, round_, user_id, raw, verdict)
    await db_session.commit()

    if not outcome.recorded:
        await message.answer("⏳ Sto ancora valutando il tentativo precedente.")
        return

    if not verdict.verified:
        # The status line matters most here: it is the proof that the outage did
        # not cost the player anything.
        await _reply(
            message,
            state,
            "⚠️ Non sono riuscito a <b>verificare</b> la tua risposta.\n"
            "Riprova: questo tentativo <b>non conta</b>.\n"
            + _status_line(round_, sess, outcome.attempts_left),
            _playing_kb(),
        )
        return

    if outcome.solved:
        tries = "1 tentativo" if outcome.attempt_no == 1 else f"{outcome.attempt_no} tentativi"
        await _end(
            message, state,
            f"🎉 <b>Indovinato!</b> In <b>{tries}</b>.\n"
            "Aspetta la chiusura per scoprire il podio! 🏆",
        )
        return

    if outcome.attempts_left <= 0:
        await _end(
            message, state,
            "❌ Tentativi <b>esauriti</b>. Ci vediamo al prossimo round!",
        )
        return

    # A wrong answer never echoes the correct one.
    lines = ["❌ <b>Non ci siamo.</b>"]
    if outcome.hint:
        lines.append(f"💡 <i>{esc(outcome.hint)}</i>")
    lines.append(_status_line(round_, sess, outcome.attempts_left))
    await _reply(message, state, "\n".join(lines), _playing_kb())


@router.callback_query(GuessPlayCb.filter(F.action == "quit"), GuessPlayStates.answering)
async def cb_quit(callback, state: FSMContext) -> None:
    """Leave the answering mode. The session (and its clock) stays: quitting is
    not a way to buy more time.

    **Not state-gated on purpose.** A «🚪 Esci» button can outlive the answering
    state — a finished game leaves its last one on screen — and a handler filtered
    on `GuessPlayStates.answering` would let that button match nothing and spin
    until Telegram gives up. Stateless, the button always answers.

    The way back goes on the message. Telling the player to «reopen the group
    link» meant scrolling the group back to an announcement that newer messages
    had already buried — a button costs one callback and no new command.
    """
    round_id = (await state.get_data()).get("round_id")
    await state.clear()
    # Strip the pressed button so it cannot be tapped a second time.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 — cosmetic, may be too old to edit
        log.debug("Tastiera del messaggio di uscita non rimovibile: %s", exc)
    if not round_id:
        # The game already ended (state was cleared): nothing to resume.
        await callback.message.answer("🚪 Uscito dal gioco.")
        await callback.answer()
        return
    b = InlineKeyboardBuilder()
    if round_id:
        b.button(
            text="🔄 Riprendi",
            callback_data=GuessPlayCb(action="resume", round_id=round_id).pack(),
        )
    await callback.message.answer(
        "🚪 Uscito dal gioco. Puoi riprendere quando vuoi — il tempo però continua a scorrere.",
        reply_markup=b.as_markup() if round_id else None,
    )
    await callback.answer()


@router.callback_query(GuessPlayCb.filter(F.action == "resume"))
async def cb_resume(
    callback, db_session: AsyncSession, state: FSMContext, callback_data: GuessPlayCb
) -> None:
    """Back into the round, through the same door the deep-link uses.

    No re-check of anything: `start_guess_session` owns every guard (finished,
    not started, already solved, out of attempts), so a second copy here would be
    a second place to forget one.
    """
    round_id = callback_data.round_id
    if round_id is None:
        await callback.answer()
        return
    await start_guess_session(
        callback.message, db_session, state, round_id, user_id=callback.from_user.id
    )
    await callback.answer()
