"""Building a guess round: three questions, then a card.

Only **title, medium and answer** are asked, because only those three have no
sensible default. Everything else starts filled in from `settings` and is one tap
away from being changed.

The shape this replaced asked eleven questions in a row with no way back: an
admin who mistyped the answer on question three could either walk the remaining
eight steps or cancel and retype everything. On an eleven-question form that is
*the* defect — not the length — and a card is what removes it. There is now no
state in which something is wrong and cannot be corrected.

Every optional field lives in `FIELDS`, which owns its label, its prompt, how to
parse it and how to render it. One edit handler serves all of them; adding a
field is a dict entry, never a new state and never a new handler.

Nothing is silently truncated: input over a cap is refused **with its real
length**, because a cut answer is only discovered once players are already
guessing against it.

The medium step keeps the shape it had. The bot **sends the medium straight
back** before accepting it — that echo *is* the check that the stored `file_id`
can be resent, made at the only moment the admin can still choose another file.

The round row is created only at publish, so an abandoned flow leaves nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
from utils.text import esc, format_seconds_short

from handlers.guess._shared import (
    _MAX_ALIAS,
    _MAX_ALIASES,
    _MAX_ANSWER,
    _MAX_ATTEMPTS_ALLOWED,
    _MAX_HINT,
    _MAX_HINTS,
    _MAX_ROUND_DURATION,
    _MAX_TIME_LIMIT,
    _MAX_TITLE,
    _MIN_ROUND_DURATION,
    _MIN_TIME_LIMIT,
    extract_media,
    kind_of,
    log,
    router,
    send_media,
    too_long,
)

_SKIP_WORDS = ("-", "no", "nessuno", "nessuna", "salta")
_HINT_SEPARATOR = "|"


class GuessCreationStates(StatesGroup):
    waiting_title = State()
    waiting_media = State()
    waiting_answer = State()
    editing = State()   # one field from the card is open for input
    card = State()      # the card is on screen, waiting for a tap


# ---------------------------------------------------------------------------
# Parsers — each returns (value, error). `data` is the flow state, so a parser
# can validate against another field (hints against the attempt limit).
# ---------------------------------------------------------------------------

def _parse_title(raw: str, _data: dict) -> tuple[str | None, str | None]:
    if len(raw) < 3:
        return None, "⚠️ Il titolo deve avere almeno 3 caratteri."
    if err := too_long(raw, _MAX_TITLE, "Il titolo è troppo lungo"):
        return None, err
    return raw, None


def _parse_answer(raw: str, _data: dict) -> tuple[str | None, str | None]:
    if len(raw) < 2:
        return None, "⚠️ La risposta deve avere almeno 2 caratteri."
    if err := too_long(raw, _MAX_ANSWER, "La risposta è troppo lunga"):
        return None, err
    return raw, None


def _parse_aliases(raw: str, _data: dict) -> tuple[list[str] | None, str | None]:
    if raw.lower() in _SKIP_WORDS:
        return [], None
    aliases = [a.strip() for a in raw.splitlines() if a.strip()]
    if len(aliases) > _MAX_ALIASES:
        return None, (f"⚠️ Massimo {_MAX_ALIASES} grafie alternative "
                      f"(ne hai mandate {len(aliases)}).")
    if any(len(a) > _MAX_ALIAS for a in aliases):
        return None, f"⚠️ Ogni grafia deve stare in {_MAX_ALIAS} caratteri."
    return aliases, None


def _bounded_int(
    raw: str, low: int, high: int, *, zero_ok: bool, unit: str
) -> tuple[int | None, str | None]:
    """One integer parser for every numeric field. `zero_ok` carries the "0 means
    no limit" convention the time fields share."""
    try:
        value = int(raw)
    except ValueError:
        return None, f"⚠️ Inserisci un numero (es. {low})."
    if zero_ok and value == 0:
        return 0, None
    if not (low <= value <= high):
        zero = " oppure 0 per nessun limite" if zero_ok else ""
        return None, f"⚠️ Il valore deve stare fra {low} e {high} {unit}{zero}."
    return value, None


def _parse_attempts(raw: str, _data: dict) -> tuple[int | None, str | None]:
    return _bounded_int(raw, 1, _MAX_ATTEMPTS_ALLOWED, zero_ok=False, unit="tentativi")


def _parse_time_limit(raw: str, _data: dict) -> tuple[int | None, str | None]:
    return _bounded_int(raw, _MIN_TIME_LIMIT, _MAX_TIME_LIMIT,
                        zero_ok=True, unit="secondi")


def _parse_duration(raw: str, _data: dict) -> tuple[int | None, str | None]:
    return _bounded_int(raw, _MIN_ROUND_DURATION, _MAX_ROUND_DURATION,
                        zero_ok=True, unit="secondi")


def _parse_prizes(raw: str, _data: dict) -> tuple[list[int] | None, str | None]:
    """All four prizes in one field. Four separate steps for four numbers of the
    same kind was four chances to mistype and no way back to the first one."""
    parts = raw.replace("/", " ").replace(",", " ").split()
    if len(parts) != 4:
        return None, ("⚠️ Servono <b>4 numeri</b>: 1°, 2°, 3° e consolazione.\n"
                      "Es. <code>800 400 200 80</code>")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        return None, "⚠️ I premi devono essere numeri. Es. <code>800 400 200 80</code>"
    if any(v < 0 for v in values):
        return None, "⚠️ Un premio non può essere negativo."
    return values, None


def _parse_hints(raw: str, data: dict) -> tuple[list[list] | None, str | None]:
    """All hints in one field, one per line: ``3 | È uno sparatutto``.

    Validated against the attempt limit *of this round*: a threshold above the
    budget is a hint nobody would ever see.
    """
    if raw.lower() in _SKIP_WORDS:
        return [], None
    max_attempts = data.get("max_attempts", settings.guess_default_attempts)
    hints: list[list] = []
    for line in (line.strip() for line in raw.splitlines()):
        if not line:
            continue
        if _HINT_SEPARATOR not in line:
            return None, (f"⚠️ Formato: <code>3 {_HINT_SEPARATOR} testo</code>, "
                          "uno per riga.")
        head, _, text = line.partition(_HINT_SEPARATOR)
        text = text.strip()
        try:
            after = int(head.strip())
        except ValueError:
            return None, (f"⚠️ Prima del «{_HINT_SEPARATOR}» ci va il numero di "
                          f"tentativi: <code>3 {_HINT_SEPARATOR} testo</code>")
        if not (1 <= after <= max_attempts):
            return None, (f"⚠️ La soglia deve stare fra 1 e <b>{max_attempts}</b> "
                          "(i tentativi di questo round): oltre, il suggerimento "
                          "non lo vedrebbe nessuno.")
        if not text:
            return None, "⚠️ Il testo del suggerimento è vuoto."
        if err := too_long(text, _MAX_HINT, "Un suggerimento è troppo lungo"):
            return None, err
        if any(a == after for a, _ in hints):
            return None, f"⚠️ Due suggerimenti sulla stessa soglia ({after})."
        hints.append([after, text])
    if len(hints) > _MAX_HINTS:
        return None, f"⚠️ Massimo {_MAX_HINTS} suggerimenti."
    return sorted(hints), None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _show_seconds(value: int, zero: str) -> str:
    return f"<b>{format_seconds_short(value)}</b>" if value else f"<i>{zero}</i>"


# ---------------------------------------------------------------------------
# The field registry — the only place a field is described.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """One editable line of the card.

    The dict key **is** the state-data key it writes, so there is nothing to look
    up and nothing to keep in sync. `apply` is the single exception, for the one
    field that writes more than one key (the four prizes); without it, every
    other field would have to return a dict to carry a single value.
    """

    label: str
    prompt: str
    parse: Callable[[str, dict], tuple[object | None, str | None]]
    show: Callable[[dict], str]
    apply: Callable[[object], dict] | None = None


FIELDS: dict[str, Field] = {
    "title": Field(
        label="📌 Titolo",
        prompt=(f"Invia il nuovo <b>titolo</b> (max {_MAX_TITLE} caratteri).\n"
                "<i>Lo vedono tutti nel gruppo: non metterci la soluzione.</i>"),
        parse=_parse_title,
        show=lambda d: f"<b>{esc(d['title'])}</b>",
    ),
    "answer": Field(
        label="✅ Risposta",
        prompt=("Qual è la <b>risposta corretta</b>? Titolo completo ed esatto "
                "(es. <code>Grand Theft Auto: San Andreas</code>).\n"
                "<i>Il giudice AI valuta tutto su questa: più è precisa, meglio "
                "giudica.</i>"),
        parse=_parse_answer,
        show=lambda d: f"<b>{esc(d['answer'])}</b>",
    ),
    "aliases": Field(
        label="🔤 Grafie accettate",
        prompt=(f"<b>Grafie alternative</b> da accettare sempre, una per riga "
                f"(max {_MAX_ALIASES}).\n"
                "Es. <code>GTA SA</code> · <code>San Andreas</code>\n"
                "<i>Accettate senza interpellare l'AI: sono la rete di sicurezza "
                "se il modello non risponde.</i>\nManda «-» per non averne."),
        parse=_parse_aliases,
        show=lambda d: (esc(", ".join(d["aliases"])) if d["aliases"]
                        else "<i>nessuna</i>"),
    ),
    "max_attempts": Field(
        label="🎯 Tentativi",
        prompt=(f"Quanti <b>tentativi</b> ha ogni giocatore? Da 1 a "
                f"{_MAX_ATTEMPTS_ALLOWED}.\n"
                "<i>Meno tentativi usa un giocatore, più in alto va nel podio.</i>"),
        parse=_parse_attempts,
        show=lambda d: f"<b>{d['max_attempts']}</b>",
    ),
    "time_limit_seconds": Field(
        label="⏱️ Tempo per giocatore",
        prompt=(f"<b>Tempo</b> per ogni giocatore, in secondi (da {_MIN_TIME_LIMIT} "
                f"a {_MAX_TIME_LIMIT}), oppure <b>0</b> per nessun limite.\n"
                "<i>Parte quando il giocatore apre il gioco, e non riparte se "
                "esce e rientra.</i>"),
        parse=_parse_time_limit,
        show=lambda d: _show_seconds(d["time_limit_seconds"], "nessun limite"),
    ),
    "round_duration_seconds": Field(
        label="⏳ Chiusura automatica",
        prompt=(f"Dopo quanto si <b>chiude da solo</b> il round, in secondi (da "
                f"{_MIN_ROUND_DURATION} a {_MAX_ROUND_DURATION}), oppure <b>0</b> "
                "per chiuderlo a mano.\n"
                "<i>Alla chiusura scattano podio, premi e reveal.</i>"),
        parse=_parse_duration,
        show=lambda d: _show_seconds(d["round_duration_seconds"], "a mano"),
    ),
    "hints": Field(
        label="💡 Suggerimenti",
        prompt=(f"<b>Suggerimenti</b>, uno per riga (max {_MAX_HINTS}):\n"
                f"<code>3 {_HINT_SEPARATOR} È uno sparatutto</code>\n"
                "<i>Arriva dopo il 3° tentativo giudicato.</i>\n"
                "Manda «-» per non averne."),
        parse=_parse_hints,
        show=lambda d: (f"<b>{len(d['hints'])}</b>" if d["hints"]
                        else "<i>nessuno</i>"),
    ),
    "prizes": Field(
        label="🏆 Premi",
        prompt=("I <b>4 premi</b> in una riga: 1°, 2°, 3° e consolazione per il 4°.\n"
                "Es. <code>800 400 200 80</code>\n"
                "<i>Dalla consolazione in giù scende fino a un minimo garantito. "
                "Metti 0 dove non vuoi premio.</i>"),
        parse=_parse_prizes,
        show=lambda d: (f"<b>{d['prize_first']}</b> / {d['prize_second']} / "
                        f"{d['prize_third']} · 4°: {d['prize_consolation']}"),
        apply=lambda v: {
            "prize_first": v[0], "prize_second": v[1],
            "prize_third": v[2], "prize_consolation": v[3],
        },
    ),
}

#: `media` is a field of the card but not of `FIELDS`: its input is a photo or an
#: audio, not text, so it routes back through `waiting_media` instead of the
#: shared text editor. It has no `parse` and no `show` — the card resends it.
_MEDIA_FIELD = "media"


def _defaults() -> dict:
    """Everything the three mandatory questions do not ask. The card opens on
    these, so the short path still produces a playable round."""
    return {
        "aliases": [],
        "hints": [],
        "max_attempts": settings.guess_default_attempts,
        "time_limit_seconds": settings.guess_default_time_limit_seconds,
        "round_duration_seconds": settings.guess_default_round_duration_seconds,
        "prize_first": settings.guess_default_first,
        "prize_second": settings.guess_default_second,
        "prize_third": settings.guess_default_third,
        "prize_consolation": settings.guess_default_consolation,
    }


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    return b.as_markup()


def _card_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, field in FIELDS.items():
        b.button(text=f"✏️ {field.label}", callback_data=f"guess_new:edit:{key}")
    b.button(text="✏️ 🖼️ Media", callback_data=f"guess_new:edit:{_MEDIA_FIELD}")
    b.button(text="✅ Pubblica", callback_data="guess_new:publish")
    b.button(text="❌ Annulla", callback_data="guess_new:cancel")
    b.adjust(2, 2, 2, 2, 1, 1, 1)
    return b.as_markup()


def _editing_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Torna alla scheda", callback_data="guess_new:back")
    return b.as_markup()


def _created_kb(kind: str, round_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Avvia ora", callback_data=f"ev:askstart:{kind}:{round_id}")
    b.button(text="🗓️ Programma", callback_data=f"ev:sched:{kind}:{round_id}")
    b.button(text="⬅️ Lista", callback_data=f"ev:list:{kind}")
    b.adjust(2, 1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# The three mandatory questions
# ---------------------------------------------------------------------------

async def start_guess_creation(
    message: Message, state: FSMContext, *, kind: str, creator_id: int
) -> None:
    """Enter the creation flow.

    `creator_id` is passed explicitly because when the events hub calls this,
    `message.from_user` is the bot and not the admin who tapped the button.
    """
    spec = kind_of(kind)
    await state.clear()
    await state.update_data(kind=kind, creator_id=creator_id, **_defaults())
    await state.set_state(GuessCreationStates.waiting_title)
    await message.answer(
        f"{spec.emoji} <b>Nuovo {esc(spec.label)}</b>\n\n"
        f"<b>1 di 3</b> — Invia il <b>titolo</b> (max {_MAX_TITLE} caratteri).\n"
        "<i>Il titolo lo vedono tutti nel gruppo: non metterci la soluzione.</i>",
        reply_markup=_cancel_kb(),
    )


@router.message(GuessCreationStates.waiting_title, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_title(message: Message, state: FSMContext) -> None:
    value, err = _parse_title((message.text or "").strip(), await state.get_data())
    if err:
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(title=value)
    spec = kind_of((await state.get_data())["kind"])
    await state.set_state(GuessCreationStates.waiting_media)
    await message.answer(
        f"<b>2 di 3</b> — {spec.media_prompt}\n"
        "<i>Te lo rimando indietro: se non ci riesco, il file non è utilizzabile "
        "e te lo dico subito.</i>",
        reply_markup=_cancel_kb(),
    )


@router.message(GuessCreationStates.waiting_media, IsAdminFilter())
async def fsm_media(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    spec = kind_of(data["kind"])
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

    # Replacing the medium from the card returns to the card; the first time
    # through, the answer is still missing and is the third question.
    if data.get("answer"):
        await _show_card(message, state)
        return
    await state.set_state(GuessCreationStates.waiting_answer)
    await message.answer(
        f"<b>3 di 3</b> — {FIELDS['answer'].prompt}",
        reply_markup=_cancel_kb(),
    )


@router.message(GuessCreationStates.waiting_answer, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_answer(message: Message, state: FSMContext) -> None:
    value, err = _parse_answer((message.text or "").strip(), await state.get_data())
    if err:
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(answer=value)
    await _show_card(message, state)


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

async def _show_card(message: Message, state: FSMContext) -> None:
    """Render the whole round, medium included, and wait for a tap.

    The medium is resent every time on purpose: seeing it next to the answer is
    how an admin notices the wrong file is attached, and each resend is one more
    proof the `file_id` is still alive.
    """
    data = await state.get_data()
    spec = kind_of(data["kind"])
    await state.set_state(GuessCreationStates.card)

    try:
        await send_media(message.bot, message.chat.id,
                         data["media_file_id"], data["media_kind"])
    except Exception as exc:  # noqa: BLE001 — worth a warning, not a dead end
        log.warning("Media non rimandabile nella scheda: %s", exc)

    lines = [f"{spec.emoji} <b>{esc(spec.label)}</b> — scheda del round\n"]
    lines += [f"{field.label}: {field.show(data)}" for field in FIELDS.values()]
    lines.append("\n<i>Tocca un campo per cambiarlo, poi pubblica.</i>")
    await message.answer("\n".join(lines), reply_markup=_card_kb())


@router.callback_query(F.data.startswith("guess_new:edit:"), IsAdminCallbackFilter())
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Open one field for input. The single entry point for every field."""
    key = callback.data.split(":")[-1]

    if key == _MEDIA_FIELD:
        spec = kind_of((await state.get_data())["kind"])
        await state.set_state(GuessCreationStates.waiting_media)
        await callback.message.answer(f"🖼️ {spec.media_prompt}",
                                      reply_markup=_editing_kb())
        await callback.answer()
        return

    field = FIELDS.get(key)
    if field is None:  # a typo in a callback must not silently edit the wrong field
        await callback.answer("Campo sconosciuto.", show_alert=True)
        return
    await state.set_state(GuessCreationStates.editing)
    await state.update_data(_editing=key)
    await callback.message.answer(f"{field.label}\n\n{field.prompt}",
                                  reply_markup=_editing_kb())
    await callback.answer()


@router.message(GuessCreationStates.editing, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_edit_value(message: Message, state: FSMContext) -> None:
    """Validate one field and go back to the card. Refused input leaves the field
    open and the previous value untouched — nothing is half-written."""
    data = await state.get_data()
    field = FIELDS.get(data.get("_editing", ""))
    if field is None:
        await _show_card(message, state)
        return

    value, err = field.parse((message.text or "").strip(), data)
    if err:
        await message.answer(err, reply_markup=_editing_kb())
        return

    update = field.apply(value) if field.apply else {data["_editing"]: value}
    await state.update_data(**update)
    await _show_card(message, state)


@router.callback_query(F.data == "guess_new:back", IsAdminCallbackFilter())
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Leave a field without changing it."""
    await _show_card(callback.message, state)
    await callback.answer()


# ---------------------------------------------------------------------------
# Publish / cancel
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "guess_new:publish", IsAdminCallbackFilter())
async def cb_publish(callback: CallbackQuery, state: FSMContext,
                     db_session: AsyncSession) -> None:
    """Create the round and arm it as ``ready``. Commits — this is a handler."""
    data = await state.get_data()
    if not data.get("answer"):  # publish tapped on a stale card
        await callback.answer("Scheda scaduta, ricomincia.", show_alert=True)
        return

    spec = kind_of(data["kind"])
    round_ = await guess_service.create_round(
        db_session,
        kind=data["kind"],
        creator_tg_id=data["creator_id"],
        title=data["title"],
        media_file_id=data["media_file_id"],
        media_kind=data["media_kind"],
        answer=data["answer"],
        aliases=data["aliases"],
        hints=[(int(a), str(t)) for a, t in data["hints"]],
        max_attempts=data["max_attempts"],
        time_limit_seconds=data["time_limit_seconds"],
        round_duration_seconds=data["round_duration_seconds"],
        prize_first=data["prize_first"],
        prize_second=data["prize_second"],
        prize_third=data["prize_third"],
        prize_consolation=data["prize_consolation"],
        group_id=group_registry.get_group_id() or None,
    )
    round_.status = "ready"
    await db_session.commit()
    await state.clear()
    await callback.message.answer(
        f"✅ <b>{esc(spec.label)} #{round_.id} creato!</b>\n\n"
        f"{spec.emoji} {esc(round_.title)}\n"
        f"🏆 {guess_service.format_prize_summary(round_)}\n\n"
        "Avvialo subito nel gruppo oppure programmalo:",
        reply_markup=_created_kb(data["kind"], round_.id),
    )
    await callback.answer()


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
