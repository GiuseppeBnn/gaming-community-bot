"""Building a guess round: the creation FSM.

One conversation from title to publish — title, medium, answer, aliases,
attempts, time limit, hints, prizes. Every input is validated against the caps in
`_shared`, and nothing is silently truncated: an over-long answer is rejected
with its real length, because a truncated one is only discovered once players are
already guessing against it.

The medium step is the one with a shape of its own. The bot **sends the medium
straight back** before accepting it — that echo is the validation that the stored
`file_id` can actually be resent, done at the only moment an admin can still pick
another file. A dead `file_id` discovered in front of the players is the worst
possible time to find out.

The round row is created only at publish, so an abandoned flow leaves nothing
behind.
"""

from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from keyboards.common_kb import confirm_cancel_kb
from services import group_registry, guess_service
from utils.text import esc

from handlers.guess._shared import (
    _MAX_ALIAS,
    _MAX_ALIASES,
    _MAX_ANSWER,
    _MAX_ATTEMPTS_ALLOWED,
    _MAX_HINT,
    _MAX_HINTS,
    _MAX_TIME_LIMIT,
    _MAX_TITLE,
    extract_media,
    kind_of,
    log,
    router,
    send_media,
    too_long,
)

_SKIP_WORDS = ("-", "no", "nessuno", "salta")
_HINT_DONE_WORDS = ("fine", "basta", "-", "no")
_HINT_SEPARATOR = "|"


class GuessCreationStates(StatesGroup):
    waiting_title = State()
    waiting_media = State()
    waiting_answer = State()
    waiting_aliases = State()
    waiting_attempts = State()
    waiting_time_limit = State()
    waiting_hints = State()
    waiting_prize_first = State()
    waiting_prize_second = State()
    waiting_prize_third = State()
    waiting_prize_consolation = State()
    reviewing = State()


# state → (data key, label, settings default attr). Table-driven exactly like the
# quiz, so the four prize steps are one handler and not four near-copies.
_PRIZE_STEPS: list[tuple[State, str, str, str]] = [
    (GuessCreationStates.waiting_prize_first, "prize_first",
     "🥇 1° classificato", "guess_default_first"),
    (GuessCreationStates.waiting_prize_second, "prize_second",
     "🥈 2° classificato", "guess_default_second"),
    (GuessCreationStates.waiting_prize_third, "prize_third",
     "🥉 3° classificato", "guess_default_third"),
    (GuessCreationStates.waiting_prize_consolation, "prize_consolation",
     "🎖️ 4° classificato (poi a scendere)", "guess_default_consolation"),
]
_PRIZE_BY_STATE = {st.state: (key, label, attr) for st, key, label, attr in _PRIZE_STEPS}


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    return b.as_markup()


def _default_kb(value: int, label: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Usa {label} ({value})", callback_data="guess_new:usedefault")
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    b.adjust(1)
    return b.as_markup()


def _skip_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ Salta", callback_data="guess_new:skip")
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    b.adjust(1)
    return b.as_markup()


def _publish_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Pubblica", callback_data="guess_new:publish")
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    b.adjust(1)
    return b.as_markup()


def _created_kb(kind: str, round_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Avvia ora", callback_data=f"ev:askstart:{kind}:{round_id}")
    b.button(text="🗓️ Programma", callback_data=f"ev:sched:{kind}:{round_id}")
    b.button(text="⬅️ Lista", callback_data=f"ev:list:{kind}")
    b.adjust(2, 1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Step prompts
# ---------------------------------------------------------------------------

async def start_guess_creation(
    message: Message, state: FSMContext, *, kind: str, creator_id: int
) -> None:
    """Enter the creation FSM.

    `creator_id` is passed explicitly because when the events hub calls this,
    `message.from_user` is the bot and not the admin who tapped the button.
    """
    spec = kind_of(kind)
    await state.clear()
    await state.update_data(kind=kind, creator_id=creator_id, hints=[], aliases=[])
    await state.set_state(GuessCreationStates.waiting_title)
    await message.answer(
        f"{spec.emoji} <b>Nuovo {esc(spec.label)}</b>\n\n"
        f"<b>Step 1</b> — Invia il <b>titolo</b> (max {_MAX_TITLE} caratteri).\n"
        "<i>Il titolo lo vedono tutti nel gruppo: non metterci la soluzione.</i>",
        reply_markup=_cancel_kb(),
    )


async def _prompt_media(message: Message, state: FSMContext) -> None:
    spec = kind_of((await state.get_data())["kind"])
    await state.set_state(GuessCreationStates.waiting_media)
    await message.answer(
        f"<b>Step 2</b> — {spec.media_prompt}\n"
        "<i>Te lo rimando indietro per conferma: se non ci riesco, il file non è "
        "utilizzabile e te lo dico subito.</i>",
        reply_markup=_cancel_kb(),
    )


async def _prompt_answer(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.waiting_answer)
    await message.answer(
        "<b>Step 3</b> — Qual è la <b>risposta corretta</b>?\n"
        "Scrivi il titolo completo ed esatto del gioco (es. "
        "<code>Grand Theft Auto: San Andreas</code>).\n"
        "<i>Su questo il giudice AI valuta tutte le risposte: più è preciso, "
        "meglio giudica.</i>",
        reply_markup=_cancel_kb(),
    )


async def _prompt_aliases(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.waiting_aliases)
    await message.answer(
        "<b>Step 4</b> — <b>Grafie alternative</b> da accettare sempre, una per riga "
        f"(max {_MAX_ALIASES}).\n"
        "Es. <code>GTA SA</code>, <code>San Andreas</code>.\n"
        "<i>Queste vengono accettate senza interpellare l'AI: sono la rete di "
        "sicurezza se il modello non è raggiungibile.</i>\n"
        "Manda «-» per saltare.",
        reply_markup=_skip_kb(),
    )


async def _prompt_attempts(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.waiting_attempts)
    await message.answer(
        "<b>Step 5</b> — Quanti <b>tentativi</b> ha ogni giocatore?\n"
        f"Invia un numero da 1 a {_MAX_ATTEMPTS_ALLOWED}.\n"
        "<i>Meno tentativi usa un giocatore, più in alto va nel podio.</i>",
        reply_markup=_default_kb(settings.guess_default_attempts, "il default"),
    )


async def _prompt_time_limit(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.waiting_time_limit)
    await message.answer(
        "<b>Step 6</b> — <b>Tempo</b> a disposizione di ogni giocatore, in secondi.\n"
        f"Da 10 a {_MAX_TIME_LIMIT}, oppure <b>0</b> per nessun limite.\n"
        "<i>Il conto parte da quando il giocatore apre il gioco in privato, e non "
        "riparte se esce e rientra.</i>",
        reply_markup=_default_kb(settings.guess_default_time_limit_seconds, "il default"),
    )


async def _prompt_hints(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.waiting_hints)
    hints = (await state.get_data()).get("hints", [])
    so_far = (
        "\n\n<b>Finora:</b>\n" + "\n".join(f"• dopo {a}: {esc(t)}" for a, t in hints)
        if hints else ""
    )
    await message.answer(
        f"<b>Step 7</b> — <b>Suggerimenti</b> (max {_MAX_HINTS}, opzionali).\n"
        f"Formato: <code>3 {_HINT_SEPARATOR} È uno sparatutto</code> — arriva dopo "
        "il 3° tentativo sbagliato.\n"
        "Manda «fine» quando hai finito." + so_far,
        reply_markup=_skip_kb(),
    )


async def _prompt_prize_step(message: Message, state: FSMContext, step: State) -> None:
    key, label, attr = _PRIZE_BY_STATE[step.state]
    default_value = getattr(settings, attr)
    await state.set_state(step)
    extra = ""
    if step == GuessCreationStates.waiting_prize_consolation:
        extra = (
            "\n\n<i>Da qui in giù la consolazione scende in modo uniforme fino a un "
            "minimo garantito per l'ultimo che indovina. 0 = nessuna consolazione.</i>"
        )
    await message.answer(
        f"💰 Premio per <b>{label}</b>\nInvia un numero (≥ 0).{extra}",
        reply_markup=_default_kb(default_value, "il default"),
    )
    # Keep the data key reachable from the "use default" button handler.
    await state.update_data(_prize_key=key)


async def _prompt_review(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    spec = kind_of(data["kind"])
    await state.set_state(GuessCreationStates.reviewing)
    aliases = data.get("aliases") or []
    hints = data.get("hints") or []
    limit = data["time_limit_seconds"]
    lines = [
        f"{spec.emoji} <b>{esc(data['title'])}</b> — riepilogo\n",
        f"✅ Risposta: <b>{esc(data['answer'])}</b>",
        f"🔤 Alias: {esc(', '.join(aliases)) if aliases else '<i>nessuno</i>'}",
        f"🎯 Tentativi: <b>{data['max_attempts']}</b>",
        f"⏱️ Tempo: <b>{limit}s</b>" if limit else "⏱️ Tempo: <i>nessun limite</i>",
        f"💡 Suggerimenti: <b>{len(hints)}</b>",
        f"🏆 {data['prize_first']} / {data['prize_second']} / {data['prize_third']} · "
        f"consolazione {data['prize_consolation']}",
    ]
    await message.answer("\n".join(lines), reply_markup=_publish_kb())


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@router.message(GuessCreationStates.waiting_title, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("⚠️ Il titolo deve avere almeno 3 caratteri.",
                             reply_markup=_cancel_kb())
        return
    if err := too_long(title, _MAX_TITLE, "Il titolo è troppo lungo"):
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(title=title)
    await _prompt_media(message, state)


@router.message(GuessCreationStates.waiting_media, IsAdminFilter())
async def fsm_media(message: Message, state: FSMContext) -> None:
    spec = kind_of((await state.get_data())["kind"])
    found = extract_media(message)
    if found is None or found[1] not in spec.accepted_media:
        await message.answer(f"⚠️ {spec.media_prompt}", reply_markup=_cancel_kb())
        return
    file_id, media_kind = found
    # Send it straight back. A file_id that cannot be resent has to fail HERE,
    # while the admin can still pick another file — not in front of the players.
    try:
        await send_media(message.bot, message.chat.id, file_id, media_kind)
    except Exception as exc:  # noqa: BLE001 — any Bot API failure means unusable
        log.warning("Anteprima media fallita in creazione: %s", exc)
        await message.answer(
            "⚠️ Non riesco a rimandarti questo file, quindi non potrei mostrarlo "
            "nemmeno ai giocatori. Mandane un altro.",
            reply_markup=_cancel_kb(),
        )
        return
    await state.update_data(media_file_id=file_id, media_kind=media_kind)
    await _prompt_answer(message, state)


@router.message(GuessCreationStates.waiting_answer, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_answer(message: Message, state: FSMContext) -> None:
    answer = (message.text or "").strip()
    if len(answer) < 2:
        await message.answer("⚠️ La risposta deve avere almeno 2 caratteri.",
                             reply_markup=_cancel_kb())
        return
    if err := too_long(answer, _MAX_ANSWER, "La risposta è troppo lunga"):
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(answer=answer)
    await _prompt_aliases(message, state)


@router.message(GuessCreationStates.waiting_aliases, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_aliases(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in _SKIP_WORDS:
        await state.update_data(aliases=[])
        await _prompt_attempts(message, state)
        return
    aliases = [a.strip() for a in raw.splitlines() if a.strip()]
    if len(aliases) > _MAX_ALIASES:
        await message.answer(f"⚠️ Massimo {_MAX_ALIASES} alias (ne hai mandati "
                             f"{len(aliases)}).", reply_markup=_skip_kb())
        return
    if any(len(a) > _MAX_ALIAS for a in aliases):
        await message.answer(f"⚠️ Ogni alias deve stare in {_MAX_ALIAS} caratteri.",
                             reply_markup=_skip_kb())
        return
    await state.update_data(aliases=aliases)
    await _prompt_attempts(message, state)


@router.message(GuessCreationStates.waiting_attempts, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_attempts(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Inserisci un numero (es. 5).",
                             reply_markup=_default_kb(settings.guess_default_attempts,
                                                      "il default"))
        return
    if not (1 <= value <= _MAX_ATTEMPTS_ALLOWED):
        await message.answer(
            f"⚠️ I tentativi devono essere fra 1 e {_MAX_ATTEMPTS_ALLOWED}.",
            reply_markup=_default_kb(settings.guess_default_attempts, "il default"),
        )
        return
    await state.update_data(max_attempts=value)
    await _prompt_time_limit(message, state)


@router.message(GuessCreationStates.waiting_time_limit, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_time_limit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer(
            "⚠️ Inserisci un numero di secondi (es. 300), oppure 0 per nessun limite.",
            reply_markup=_default_kb(settings.guess_default_time_limit_seconds,
                                     "il default"),
        )
        return
    if value != 0 and not (10 <= value <= _MAX_TIME_LIMIT):
        await message.answer(
            f"⚠️ Il tempo deve essere 0 (nessun limite) oppure fra 10 e "
            f"{_MAX_TIME_LIMIT} secondi.",
            reply_markup=_default_kb(settings.guess_default_time_limit_seconds,
                                     "il default"),
        )
        return
    await state.update_data(time_limit_seconds=value)
    await _prompt_hints(message, state)


@router.message(GuessCreationStates.waiting_hints, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_hint(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    hints: list = list(data.get("hints", []))

    if raw.lower() in _HINT_DONE_WORDS:
        await state.update_data(hints=hints)
        await _prompt_prize_step(message, state, GuessCreationStates.waiting_prize_first)
        return

    if _HINT_SEPARATOR not in raw:
        await message.answer(
            f"⚠️ Formato: <code>3 {_HINT_SEPARATOR} testo del suggerimento</code>, "
            "oppure «fine».",
            reply_markup=_skip_kb(),
        )
        return
    head, _, text = raw.partition(_HINT_SEPARATOR)
    text = text.strip()
    try:
        after = int(head.strip())
    except ValueError:
        await message.answer(
            f"⚠️ Prima del «{_HINT_SEPARATOR}» ci va il numero di tentativi.",
            reply_markup=_skip_kb(),
        )
        return
    max_attempts = data["max_attempts"]
    if not (1 <= after <= max_attempts):
        # A hint that unlocks past the attempt limit is a hint nobody ever sees.
        await message.answer(
            f"⚠️ La soglia deve stare fra 1 e <b>{max_attempts}</b> (i tentativi "
            "di questo round): oltre, il suggerimento non lo vedrebbe nessuno.",
            reply_markup=_skip_kb(),
        )
        return
    if not text:
        await message.answer("⚠️ Il suggerimento è vuoto.", reply_markup=_skip_kb())
        return
    if err := too_long(text, _MAX_HINT, "Il suggerimento è troppo lungo"):
        await message.answer(err, reply_markup=_skip_kb())
        return
    if len(hints) >= _MAX_HINTS:
        await message.answer(f"⚠️ Massimo {_MAX_HINTS} suggerimenti. Manda «fine».",
                             reply_markup=_skip_kb())
        return
    if any(a == after for a, _ in hints):
        await message.answer(f"⚠️ C'è già un suggerimento dopo {after} tentativi.",
                             reply_markup=_skip_kb())
        return

    hints.append((after, text))
    await state.update_data(hints=hints)
    await _prompt_hints(message, state)


@router.message(GuessCreationStates.waiting_prize_first, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(GuessCreationStates.waiting_prize_second, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(GuessCreationStates.waiting_prize_third, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(GuessCreationStates.waiting_prize_consolation, IsAdminFilter(),
                ~F.text.startswith("/"))
async def fsm_prize_value(message: Message, state: FSMContext) -> None:
    key, _label, attr = _PRIZE_BY_STATE[await state.get_state()]
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Inserisci un numero (es. 500, oppure 0).",
                             reply_markup=_default_kb(getattr(settings, attr), "il default"))
        return
    if value < 0:
        await message.answer("⚠️ Il premio non può essere negativo.",
                             reply_markup=_default_kb(getattr(settings, attr), "il default"))
        return
    await _advance_prize(message, state, key, value)


async def _advance_prize(message: Message, state: FSMContext, key: str, value: int) -> None:
    await state.update_data(**{key: value})
    states = [s.state for s, *_ in _PRIZE_STEPS]
    idx = states.index(await state.get_state())
    if idx + 1 < len(states):
        await _prompt_prize_step(message, state, _PRIZE_STEPS[idx + 1][0])
    else:
        await _prompt_review(message, state)


# ---------------------------------------------------------------------------
# Buttons: default / skip / publish / cancel
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "guess_new:usedefault", IsAdminCallbackFilter())
async def cb_use_default(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current == GuessCreationStates.waiting_attempts.state:
        await state.update_data(max_attempts=settings.guess_default_attempts)
        await _prompt_time_limit(callback.message, state)
    elif current == GuessCreationStates.waiting_time_limit.state:
        await state.update_data(
            time_limit_seconds=settings.guess_default_time_limit_seconds
        )
        await _prompt_hints(callback.message, state)
    elif current in _PRIZE_BY_STATE:
        key, _label, attr = _PRIZE_BY_STATE[current]
        await _advance_prize(callback.message, state, key, getattr(settings, attr))
    await callback.answer()


@router.callback_query(F.data == "guess_new:skip", IsAdminCallbackFilter())
async def cb_skip(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current == GuessCreationStates.waiting_aliases.state:
        await state.update_data(aliases=[])
        await _prompt_attempts(callback.message, state)
    elif current == GuessCreationStates.waiting_hints.state:
        await _prompt_prize_step(callback.message, state,
                                 GuessCreationStates.waiting_prize_first)
    await callback.answer()


@router.callback_query(GuessCreationStates.reviewing, F.data == "guess_new:publish",
                       IsAdminCallbackFilter())
async def cb_publish(callback: CallbackQuery, state: FSMContext,
                     db_session: AsyncSession) -> None:
    await fsm_publish(callback.message, state, db_session)
    await callback.answer()


async def fsm_publish(message: Message, state: FSMContext,
                      db_session: AsyncSession) -> None:
    """Create the round and arm it as ``ready``. Commits — this is a handler."""
    data = await state.get_data()
    spec = kind_of(data["kind"])
    round_ = await guess_service.create_round(
        db_session,
        kind=data["kind"],
        creator_tg_id=data["creator_id"],
        title=data["title"],
        media_file_id=data["media_file_id"],
        media_kind=data["media_kind"],
        answer=data["answer"],
        aliases=data.get("aliases") or [],
        hints=[(a, t) for a, t in data.get("hints") or []],
        max_attempts=data["max_attempts"],
        time_limit_seconds=data["time_limit_seconds"],
        prize_first=data["prize_first"],
        prize_second=data["prize_second"],
        prize_third=data["prize_third"],
        prize_consolation=data["prize_consolation"],
        group_id=group_registry.get_group_id() or None,
    )
    round_.status = "ready"
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>{esc(spec.label)} #{round_.id} creato!</b>\n\n"
        f"{spec.emoji} {esc(round_.title)}\n"
        f"🏆 {guess_service.format_prize_summary(round_)}\n\n"
        "Avvialo subito nel gruppo oppure programmalo:",
        reply_markup=_created_kb(data["kind"], round_.id),
    )


@router.callback_query(F.data == "guess_new:cancel", IsAdminCallbackFilter())
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb("guess_new:cancel_yes", "guess_new:cancel_no"),
    )
    await callback.answer()


@router.callback_query(F.data == "guess_new:cancel_yes", IsAdminCallbackFilter())
async def cb_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione annullata.")
    await callback.answer()


@router.callback_query(F.data == "guess_new:cancel_no", IsAdminCallbackFilter())
async def cb_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()
