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

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.callbacks import EventCb, GuessNewCb
from keyboards.common_kb import confirm_cancel_kb
from services import group_registry, guess_service, schedule_service
from utils.text import esc, format_seconds_short

from handlers.guess._shared import (
    forget_message,
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

#: Ways to say "none" in the one field that still takes free text (aliases).
_SKIP_WORDS = ("-", "no", "nessuno", "nessuna", "salta")


class GuessCreationStates(StatesGroup):
    waiting_title = State()
    waiting_media = State()
    waiting_answer = State()
    editing = State()  # one field from the card is open for input
    card = State()  # the card is on screen, waiting for a tap
    hints = State()  # the hints screen is open
    waiting_hint_text = State()  # a hint's words, before its threshold


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
        return None, (
            f"⚠️ Massimo {_MAX_ALIASES} grafie alternative (ne hai mandate {len(aliases)})."
        )
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


#: Whole minutes for the per-player time: '5m', '5 min', '5 minuti'. Unambiguous
#: here (a plain per-player duration), unlike the auto-close field.
_MINUTES_RE = re.compile(r"^(\d+)\s*(?:m|min|minuti|minuto)$", re.IGNORECASE)


def _parse_time_limit(raw: str, _data: dict) -> tuple[int | None, str | None]:
    """Player time in seconds, or in whole minutes ('5m'/'5 min'). 0 = no limit."""
    raw = raw.strip()
    m = _MINUTES_RE.match(raw)
    if m:
        seconds = int(m.group(1)) * 60
        if seconds == 0:
            return 0, None
        if not (_MIN_TIME_LIMIT <= seconds <= _MAX_TIME_LIMIT):
            lo = -(-_MIN_TIME_LIMIT // 60)  # ceil → smallest whole minute in range
            hi = _MAX_TIME_LIMIT // 60
            return None, (f"⚠️ Il tempo in minuti deve stare fra {lo} e {hi} "
                          "(oppure 0 per nessun limite).")
        return seconds, None
    return _bounded_int(raw, _MIN_TIME_LIMIT, _MAX_TIME_LIMIT,
                        zero_ok=True, unit="secondi")


#: A bare relative token (30m/2h/1d). Refused in the auto-close field on purpose:
#: "from now" or "from start"? — the two accepted forms (seconds, absolute date)
#: are unambiguous, so the ambiguous one is turned away rather than guessed.
_REL_TOKEN_RE = re.compile(r"^\d+\s*[mhd]$", re.IGNORECASE)


def _parse_close(raw: str, _data: dict) -> tuple[dict | None, str | None]:
    """The auto-close, in either of two unambiguous shapes.

    A plain integer keeps the original meaning — seconds after the round *starts*
    (relative, armed at open-time). Anything else is read as an absolute
    ``AAAA-MM-GG HH:MM`` instant, parsed by the same helper as ``/programma``. The
    two are mutually exclusive, so this writes **both** state keys at once (like
    the four prizes) and its ``apply`` is the identity.
    """
    raw = raw.strip()
    try:
        int(raw)
    except ValueError:
        pass
    else:
        value, err = _bounded_int(raw, _MIN_ROUND_DURATION, _MAX_ROUND_DURATION,
                                  zero_ok=True, unit="secondi")
        if err:
            return None, err
        return {"round_duration_seconds": value, "round_closes_at": None}, None

    if _REL_TOKEN_RE.match(raw):
        return None, ("⚠️ Per una <b>durata</b> usa i secondi (es. "
                      "<code>1800</code>); per una <b>data</b> usa "
                      "<code>AAAA-MM-GG HH:MM</code>.")
    try:
        dt = schedule_service.parse_run_at(raw)
    except ValueError as e:
        return None, f"⚠️ {e}"
    return {"round_duration_seconds": 0, "round_closes_at": dt.isoformat()}, None


def _parse_prizes(raw: str, _data: dict) -> tuple[list[int] | None, str | None]:
    """All four prizes in one field. Four separate steps for four numbers of the
    same kind was four chances to mistype and no way back to the first one."""
    parts = raw.replace("/", " ").replace(",", " ").split()
    if len(parts) != 4:
        return None, (
            "⚠️ Servono <b>4 numeri</b>: 1°, 2°, 3° e consolazione.\nEs. <code>800 400 200 80</code>"
        )
    try:
        values = [int(p) for p in parts]
    except ValueError:
        return None, "⚠️ I premi devono essere numeri. Es. <code>800 400 200 80</code>"
    if any(v < 0 for v in values):
        return None, "⚠️ Un premio non può essere negativo."
    return values, None


def hints_of(data: dict) -> list[list]:
    """The hints held in the flow state, always sorted by threshold."""
    return sorted(data.get("hints") or [])


def free_thresholds(data: dict) -> list[int]:
    """Attempt numbers that do not have a hint yet.

    The single source of truth for **which numbers may be chosen**. The keyboard
    renders it and the callback re-checks against it, so "offered" and "accepted"
    can never drift apart — the property that makes a forged callback harmless.
    """
    taken = {int(after) for after, _ in hints_of(data)}
    limit = int(data.get("max_attempts", settings.guess_default_attempts))
    return [n for n in range(1, limit + 1) if n not in taken]


def prune_unreachable_hints(data: dict) -> list[list]:
    """Drop hints whose threshold is above the current attempt limit.

    The threshold used to be validated only as it was typed, so setting 10
    attempts, adding a hint at 8 and then lowering to 3 left a hint no player
    could ever reach — exactly what that validation exists to prevent. The limit
    is editable, so the check has to run when the limit changes, not only when
    the hint is written.
    """
    limit = int(data.get("max_attempts", settings.guess_default_attempts))
    return [h for h in hints_of(data) if int(h[0]) <= limit]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _show_seconds(value: int, zero: str) -> str:
    return f"<b>{format_seconds_short(value)}</b>" if value else f"<i>{zero}</i>"


def _show_close(d: dict) -> str:
    """The auto-close line: an absolute date when one was picked, otherwise the
    relative duration (or «a mano» when there is none)."""
    iso = d.get("round_closes_at")
    if iso:
        local = schedule_service.to_local(datetime.fromisoformat(iso))
        return f"<b>{local:%d/%m/%Y %H:%M}</b>"
    return _show_seconds(d["round_duration_seconds"], "a mano")


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
    #: ``None`` means the field is not edited by typing — it has a screen of its
    #: own (hints). The card still renders and buttons it like any other line.
    parse: Callable[[str, dict], tuple[object | None, str | None]] | None
    show: Callable[[dict], str]
    apply: Callable[[object], dict] | None = None


FIELDS: dict[str, Field] = {
    "title": Field(
        label="📌 Titolo",
        prompt=(
            f"Invia il nuovo <b>titolo</b> (max {_MAX_TITLE} caratteri).\n"
            "<i>Lo vedono tutti nel gruppo: non metterci la soluzione.</i>"
        ),
        parse=_parse_title,
        show=lambda d: f"<b>{esc(d['title'])}</b>",
    ),
    "answer": Field(
        label="✅ Risposta",
        prompt=(
            "Qual è la <b>risposta corretta</b>? Titolo completo ed esatto "
            "(es. <code>Grand Theft Auto: San Andreas</code>).\n"
            "<i>Il giudice AI valuta tutto su questa: più è precisa, meglio "
            "giudica.</i>"
        ),
        parse=_parse_answer,
        show=lambda d: f"<b>{esc(d['answer'])}</b>",
    ),
    "aliases": Field(
        label="🔤 Grafie accettate",
        prompt=(
            f"<b>Grafie alternative</b> da accettare sempre, una per riga "
            f"(max {_MAX_ALIASES}).\n"
            "Es. <code>GTA SA</code> · <code>San Andreas</code>\n"
            "<i>Accettate senza interpellare l'AI: sono la rete di sicurezza "
            "se il modello non risponde.</i>\nManda «-» per non averne."
        ),
        parse=_parse_aliases,
        show=lambda d: esc(", ".join(d["aliases"])) if d["aliases"] else "<i>nessuna</i>",
    ),
    "max_attempts": Field(
        label="🎯 Tentativi",
        prompt=(
            f"Quanti <b>tentativi</b> ha ogni giocatore? Da 1 a "
            f"{_MAX_ATTEMPTS_ALLOWED}.\n"
            "<i>Meno tentativi usa un giocatore, più in alto va nel podio.</i>"
        ),
        parse=_parse_attempts,
        show=lambda d: f"<b>{d['max_attempts']}</b>",
    ),
    "time_limit_seconds": Field(
        label="⏱️ Tempo per giocatore",
        prompt=(f"<b>Tempo</b> per ogni giocatore: in <b>secondi</b> (da "
                f"{_MIN_TIME_LIMIT} a {_MAX_TIME_LIMIT}) o in <b>minuti</b> "
                "(es. <code>5m</code>), oppure <b>0</b> per nessun limite.\n"
                "<i>Parte quando il giocatore apre il gioco, e non riparte se "
                "esce e rientra.</i>"),
        parse=_parse_time_limit,
        show=lambda d: _show_seconds(d["time_limit_seconds"], "nessun limite"),
    ),
    "round_duration_seconds": Field(
        label="⏳ Chiusura automatica",
        prompt=(f"Dopo quanti <b>secondi</b> dall'avvio si <b>chiude da solo</b> il "
                f"round (da {_MIN_ROUND_DURATION} a {_MAX_ROUND_DURATION}), oppure "
                "una <b>data</b> <code>AAAA-MM-GG HH:MM</code>, oppure <b>0</b> per "
                "chiuderlo a mano.\n"
                "<i>Alla chiusura scattano podio, premi e reveal.</i>"),
        parse=_parse_close,
        show=_show_close,
        # `parse` already returns the two state keys; nothing more to map.
        apply=lambda v: v,
    ),
    # No `parse`: hints are built on their own screen, from buttons. See
    # `_hints_panel`. The entry stays in FIELDS so the card still renders and
    # buttons it like every other line.
    "hints": Field(
        label="💡 Suggerimenti",
        prompt="",
        parse=None,
        show=lambda d: f"<b>{len(d['hints'])}</b>" if d["hints"] else "<i>nessuno</i>",
    ),
    "prizes": Field(
        label="🏆 Premi",
        prompt=(
            "I <b>4 premi</b> in una riga: 1°, 2°, 3° e consolazione per il 4°.\n"
            "Es. <code>800 400 200 80</code>\n"
            "<i>Dalla consolazione in giù scende fino a un minimo garantito. "
            "Metti 0 dove non vuoi premio.</i>"
        ),
        parse=_parse_prizes,
        show=lambda d: (
            f"<b>{d['prize_first']}</b> / {d['prize_second']} / "
            f"{d['prize_third']} · 4°: {d['prize_consolation']}"
        ),
        apply=lambda v: {
            "prize_first": v[0],
            "prize_second": v[1],
            "prize_third": v[2],
            "prize_consolation": v[3],
        },
    ),
}

#: `media` is a field of the card but not of `FIELDS`: its input is a photo or an
#: audio, not text, so it routes back through `waiting_media` instead of the
#: shared text editor. It has no `parse` and no `show` — the card resends it.
_MEDIA_FIELD = "media"
#: Built on its own screen from buttons, not typed. See `_hints_panel`.
_HINTS_FIELD = "hints"


def _defaults() -> dict:
    """Everything the three mandatory questions do not ask. The card opens on
    these, so the short path still produces a playable round."""
    return {
        "aliases": [],
        "hints": [],
        "max_attempts": settings.guess_default_attempts,
        "time_limit_seconds": settings.guess_default_time_limit_seconds,
        "round_duration_seconds": settings.guess_default_round_duration_seconds,
        "round_closes_at": None,
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
    b.button(text="❌ Annulla", callback_data=GuessNewCb(action="cancel").pack())
    return b.as_markup()


def _card_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, field in FIELDS.items():
        b.button(text=f"✏️ {field.label}", callback_data=GuessNewCb(action="edit", key=key).pack())
    b.button(text="✏️ 🖼️ Media", callback_data=GuessNewCb(action="edit", key=_MEDIA_FIELD).pack())
    b.button(text="✅ Pubblica", callback_data=GuessNewCb(action="publish").pack())
    b.button(text="❌ Annulla", callback_data=GuessNewCb(action="cancel").pack())
    b.adjust(2, 2, 2, 2, 1, 1, 1)
    return b.as_markup()


def _editing_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Torna alla scheda", callback_data=GuessNewCb(action="back").pack())
    return b.as_markup()


def _created_kb(kind: str, round_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text="▶️ Avvia ora",
        callback_data=EventCb(action="askstart", task_type=kind, item_id=round_id).pack(),
    )
    b.button(
        text="🗓️ Programma",
        callback_data=EventCb(action="sched", task_type=kind, item_id=round_id).pack(),
    )
    b.button(text="⬅️ Lista", callback_data=EventCb(action="list", task_type=kind).pack())
    b.adjust(2, 1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# The three mandatory questions
# ---------------------------------------------------------------------------

_TITLE_PROMPT = (
    f"<b>1 di 3</b> — Invia il <b>titolo</b> (max {_MAX_TITLE} caratteri).\n"
    "<i>Il titolo lo vedono tutti nel gruppo: non metterci la soluzione.</i>"
)


def _step_prompt(data: dict) -> tuple[State, str] | None:
    """The mandatory question still unanswered, or None when all three are in.

    One place decides *where the flow is*, so nothing can send the admin to a card
    that cannot be rendered yet — the three `show` lambdas read `title`, `answer`
    and the rest straight out of the state data.
    """
    spec = kind_of(data["kind"])
    if not data.get("title"):
        return GuessCreationStates.waiting_title, _TITLE_PROMPT
    if not data.get("media_file_id"):
        return GuessCreationStates.waiting_media, (
            f"<b>2 di 3</b> — {spec.media_prompt}\n"
            "<i>Te lo rimando indietro: se non ci riesco, il file non è utilizzabile "
            "e te lo dico subito.</i>"
        )
    if not data.get("answer"):
        return GuessCreationStates.waiting_answer, f"<b>3 di 3</b> — {FIELDS['answer'].prompt}"
    return None


async def _ask_next(message: Message, state: FSMContext, *, in_panel: bool = False) -> None:
    """Ask the pending mandatory question, or show the card when there is none.

    `in_panel` reuses the card message instead of sending a new one: the caller is
    replacing a screen that is already there (the «sei sicuro?» confirmation).
    """
    step = _step_prompt(await state.get_data())
    if step is None:
        await _show_card(message, state)
        return
    next_state, prompt = step
    await state.set_state(next_state)
    if in_panel:
        await _panel(message, state, prompt, _cancel_kb())
    else:
        await message.answer(prompt, reply_markup=_cancel_kb())


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
        f"{spec.emoji} <b>Nuovo {esc(spec.label)}</b>\n\n{_TITLE_PROMPT}",
        reply_markup=_cancel_kb(),
    )


@router.message(GuessCreationStates.waiting_title, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_title(message: Message, state: FSMContext) -> None:
    value, err = _parse_title((message.text or "").strip(), await state.get_data())
    if err:
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(title=value)
    await _ask_next(message, state)


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
    # This is also the ONLY place the medium is posted: the card no longer resends
    # it on every render.
    try:
        echo = await send_media(message.bot, message.chat.id, file_id, media_kind)
    except Exception as exc:  # noqa: BLE001 — any Bot API failure means unusable
        log.warning("Anteprima media fallita in creazione: %s", exc)
        await message.answer(
            "⚠️ Non riesco a rimandarti questo file, quindi non potrei mostrarlo "
            "nemmeno ai giocatori. Mandane un altro.",
            reply_markup=_cancel_kb(),
        )
        return
    # Drop the previous one: two media in the chat and the admin cannot tell which
    # of them the round actually uses.
    await forget_message(message.bot, message.chat.id, data.get("media_message_id"))
    # The panel has to come back UNDER the medium. This is the only step that puts
    # new messages in the chat (the upload and its echo), so editing the card in
    # place would leave it *above* them: the admin is looking at a photo with no
    # buttons, and replacing the medium from the card looks like a dead end.
    # Everywhere else the panel stays last because the typed message is deleted.
    await forget_message(message.bot, message.chat.id, data.get("card_message_id"))
    await state.update_data(
        media_file_id=file_id,
        media_kind=media_kind,
        media_message_id=echo.message_id,
        card_message_id=None,
    )

    # Replacing the medium from the card returns to the card; the first time
    # through, the answer is still missing and is the third question.
    await _ask_next(message, state)


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


async def _panel(
    message: Message, state: FSMContext, text: str, kb: InlineKeyboardMarkup | None
) -> None:
    """Put `text` on **the** card message, editing it in place.

    The card is a panel, not a feed. Posting a fresh one for every change buried
    the chat in stale copies whose buttons still looked live, and — because each
    render also resent the medium — fired a media upload per edit, which Telegram
    rate-limits: the bot appeared to freeze. One message, edited, fixes the mess
    and the stall with the same change.

    Falls back to sending when there is nothing to edit yet, or when the edit is
    refused (message too old, deleted, or identical content — Telegram treats a
    no-op edit as an error).
    """
    data = await state.get_data()
    message_id = data.get("card_message_id")
    if message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message_id,
                text=text,
                reply_markup=kb,
            )
            return
        except Exception as exc:  # noqa: BLE001 — fall back to a fresh panel
            log.debug("Scheda %s non modificabile, ne mando una nuova: %s", message_id, exc)
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(card_message_id=sent.message_id)


async def _show_card(message: Message, state: FSMContext, *, note: str = "") -> None:
    """Render the whole round and wait for a tap.

    The medium is **not** resent here — it is posted once, when it is chosen or
    replaced, and stays above the card. Resending it per render was what made the
    chat unusable and the bot stall.

    `note` carries a one-off remark about what the last edit did beyond setting
    its own field, so a silent side effect (hints dropped because the attempt
    limit shrank) is stated where the admin is already looking.
    """
    data = await state.get_data()
    spec = kind_of(data["kind"])
    await state.set_state(GuessCreationStates.card)

    lines = [f"{spec.emoji} <b>{esc(spec.label)}</b> — scheda del round\n"]
    lines += [f"{field.label}: {field.show(data)}" for field in FIELDS.values()]
    lines.append("\n<i>Tocca un campo per cambiarlo, poi pubblica.</i>")
    await _panel(message, state, "\n".join(lines) + note, _card_kb())


@router.callback_query(GuessNewCb.filter(F.action == "edit"), IsAdminCallbackFilter())
async def cb_edit(callback: CallbackQuery, state: FSMContext, callback_data: GuessNewCb) -> None:
    """Open one field for input. The single entry point for every field.

    Leaving anywhere drops a half-written hint: text typed but never given a
    threshold must not be able to attach itself later, when a stale number button
    from that screen gets tapped.
    """
    key = callback_data.key
    if key is None:
        await callback.answer("Campo sconosciuto.", show_alert=True)
        return
    await state.update_data(_pending_hint=None)

    if key == _MEDIA_FIELD:
        spec = kind_of((await state.get_data())["kind"])
        await state.set_state(GuessCreationStates.waiting_media)
        await _panel(callback.message, state, f"🖼️ {spec.media_prompt}", _editing_kb())
        await callback.answer()
        return

    if key == _HINTS_FIELD:
        await _hints_panel(callback.message, state)
        await callback.answer()
        return

    field = FIELDS.get(key)
    # A typo in a callback must not silently edit the wrong field; a field with no
    # `parse` has a screen of its own and must never reach the text editor.
    if field is None or field.parse is None:
        await callback.answer("Campo sconosciuto.", show_alert=True)
        return
    await state.set_state(GuessCreationStates.editing)
    await state.update_data(_editing=key)
    # The prompt REPLACES the card rather than stacking under it, so there is only
    # ever one live panel on screen.
    await _panel(callback.message, state, f"{field.label}\n\n{field.prompt}", _editing_kb())
    await callback.answer()


@router.message(GuessCreationStates.editing, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_edit_value(message: Message, state: FSMContext) -> None:
    """Validate one field and go back to the card. Refused input leaves the field
    open and the previous value untouched — nothing is half-written."""
    data = await state.get_data()
    field = FIELDS.get(data.get("_editing", ""))
    if field is None or field.parse is None:
        await _show_card(message, state)
        return

    value, err = field.parse((message.text or "").strip(), data)
    if err:
        await _panel(message, state, f"{field.label}\n\n{field.prompt}\n\n{err}", _editing_kb())
        return

    update = field.apply(value) if field.apply else {data["_editing"]: value}
    await state.update_data(**update)
    # The typed value has been absorbed into the card; leaving it on screen turns
    # the wizard back into the feed this replaced.
    await forget_message(message.bot, message.chat.id, message.message_id)

    # Lowering the attempt limit can strand hints above it. The threshold is only
    # checked when a hint is written, and the limit is editable afterwards — so
    # without this, 10 attempts + a hint at 8 + "actually, 3 attempts" ships a
    # hint no player can reach, which is the exact thing that check prevents.
    note = ""
    if data["_editing"] == "max_attempts":
        kept = prune_unreachable_hints(await state.get_data())
        dropped = len(hints_of(await state.get_data())) - len(kept)
        if dropped:
            await state.update_data(hints=kept)
            subject = "<b>1</b> suggerimento" if dropped == 1 else f"<b>{dropped}</b> suggerimenti"
            seen = "non lo avrebbe visto" if dropped == 1 else "non li avrebbe visti"
            note = (
                f"\n\n⚠️ Ho tolto {subject}: la soglia era oltre i "
                f"<b>{value}</b> tentativi, quindi {seen} nessuno."
            )

    await _show_card(message, state, note=note)


# ---------------------------------------------------------------------------
# Hints — a screen of its own, driven by buttons
#
# The old field wanted `3 | È uno sparatutto` typed by hand: a separator
# character, an argument order and a magic number. Three things an admin who does
# not write code has no reason to guess, and three ways to get an error message
# instead of a hint.
#
# The threshold is now a button. That is the intuitive half; the safe half is
# that `free_thresholds` is the *only* source of valid numbers and every callback
# re-checks against it, so a stale or forged `guess_new:hint_at::99` creates
# nothing. The only free text left is the hint body, which needs a length check
# and nothing else — a value that never passes through a parser cannot be
# malformed.
# ---------------------------------------------------------------------------


def _hints_kb(data: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    hints = hints_of(data)
    if len(hints) < _MAX_HINTS and free_thresholds(data):
        b.button(text="➕ Aggiungi", callback_data=GuessNewCb(action="hint_add").pack())
    if hints:
        b.button(text="↩️ Togli l'ultimo", callback_data=GuessNewCb(action="hint_undo").pack())
        b.button(text="🧹 Cancella tutti", callback_data=GuessNewCb(action="hint_clear").pack())
    b.button(text="⬅️ Fatto", callback_data=GuessNewCb(action="hint_done").pack())
    b.adjust(1, 2, 1)
    return b.as_markup()


def _thresholds_kb(data: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in free_thresholds(data):
        b.button(text=str(n), callback_data=GuessNewCb(action="hint_at", value=n).pack())
    b.adjust(5)
    # Its own row: packed in with the numbers it reads as one of the choices, and
    # sits where a mistap costs the hint you just typed.
    b.row(
        InlineKeyboardButton(
            text="❌ Lascia perdere", callback_data=GuessNewCb(action="hint_done").pack()
        )
    )
    return b.as_markup()


def _hints_text(data: dict) -> str:
    hints = hints_of(data)
    lines = [f"💡 <b>Suggerimenti</b> — {len(hints)} su {_MAX_HINTS}\n"]
    if hints:
        lines += [f"• dopo <b>{after}</b> tentativi: {esc(text)}" for after, text in hints]
    else:
        lines.append("<i>Ancora nessuno.</i>")
    lines.append(
        "\n<i>Un suggerimento arriva quando il giocatore ha usato quel numero di "
        "tentativi senza indovinare.</i>"
    )
    return "\n".join(lines)


async def _hints_panel(message: Message, state: FSMContext) -> None:
    await state.set_state(GuessCreationStates.hints)
    data = await state.get_data()
    await _panel(message, state, _hints_text(data), _hints_kb(data))


@router.callback_query(GuessNewCb.filter(F.action == "hint_add"), IsAdminCallbackFilter())
async def cb_hint_add(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if len(hints_of(data)) >= _MAX_HINTS:
        await _panel(
            callback.message,
            state,
            f"{_hints_text(data)}\n\n⚠️ Massimo {_MAX_HINTS} suggerimenti.",
            _hints_kb(data),
        )
        await callback.answer()
        return
    if not free_thresholds(data):
        await _panel(
            callback.message,
            state,
            f"{_hints_text(data)}\n\n⚠️ Ogni tentativo ha già il suo suggerimento.",
            _hints_kb(data),
        )
        await callback.answer()
        return
    await state.set_state(GuessCreationStates.waiting_hint_text)
    await _panel(
        callback.message,
        state,
        "💡 Scrivi il <b>suggerimento</b>.\n"
        "<i>Poi ti chiedo dopo quanti tentativi farlo arrivare.</i>",
        _editing_kb(),
    )
    await callback.answer()


@router.message(GuessCreationStates.waiting_hint_text, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_hint_text(message: Message, state: FSMContext) -> None:
    """Take the hint's words, then ask *when* with buttons."""
    text = (message.text or "").strip()
    if not text:
        await _panel(
            message, state, "⚠️ Il suggerimento è vuoto. Scrivilo e te lo salvo.", _editing_kb()
        )
        return
    if err := too_long(text, _MAX_HINT, "Il suggerimento è troppo lungo"):
        await _panel(message, state, err, _editing_kb())
        return

    await state.update_data(_pending_hint=text)
    await forget_message(message.bot, message.chat.id, message.message_id)
    data = await state.get_data()
    await _panel(
        message,
        state,
        f"💡 «{esc(text)}»\n\n<b>Dopo quanti tentativi</b> sbagliati deve arrivare?",
        _thresholds_kb(data),
    )


@router.callback_query(GuessNewCb.filter(F.action == "hint_at"), IsAdminCallbackFilter())
async def cb_hint_at(callback: CallbackQuery, state: FSMContext, callback_data: GuessNewCb) -> None:
    """Attach the pending hint to a threshold.

    Everything here is re-checked even though the keyboard only offered valid
    numbers: callback data is user input, and a button from a screen taken three
    edits ago is as untrusted as a forged one.
    """
    data = await state.get_data()
    pending = data.get("_pending_hint")
    if not pending:  # a stale button must not invent a hint out of nothing
        await _hints_panel(callback.message, state)
        await callback.answer()
        return

    if len(hints_of(data)) >= _MAX_HINTS:  # belt and braces: `add` guards this too
        await callback.answer(f"Massimo {_MAX_HINTS} suggerimenti.", show_alert=True)
        return

    after = callback_data.value
    if after is None:
        await callback.answer("Valore non valido.", show_alert=True)
        return
    if after not in free_thresholds(data):
        # Out of range, or taken since this keyboard was drawn.
        await callback.answer("Quel numero non è disponibile.", show_alert=True)
        await _hints_panel(callback.message, state)
        return

    await state.update_data(hints=sorted([*hints_of(data), [after, pending]]), _pending_hint=None)
    await _hints_panel(callback.message, state)
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "hint_undo"), IsAdminCallbackFilter())
async def cb_hint_undo(callback: CallbackQuery, state: FSMContext) -> None:
    hints = hints_of(await state.get_data())
    await state.update_data(hints=hints[:-1])
    await _hints_panel(callback.message, state)
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "hint_clear"), IsAdminCallbackFilter())
async def cb_hint_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(hints=[], _pending_hint=None)
    await _hints_panel(callback.message, state)
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "hint_done"), IsAdminCallbackFilter())
async def cb_hint_done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(_pending_hint=None)
    await _show_card(callback.message, state)
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "back"), IsAdminCallbackFilter())
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Leave a field without changing it."""
    await _show_card(callback.message, state)
    await callback.answer()


# ---------------------------------------------------------------------------
# Publish / cancel
# ---------------------------------------------------------------------------


@router.callback_query(GuessNewCb.filter(F.action == "publish"), IsAdminCallbackFilter())
async def cb_publish(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
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
        # Pruned once more on the way out. Every path into `hints` is already
        # guarded, so this changes nothing today — it is here so a future path
        # that sets `max_attempts` without pruning cannot quietly ship a hint no
        # player can reach. Last gate before the data becomes a round that pays.
        hints=[(int(a), str(t)) for a, t in prune_unreachable_hints(data)],
        max_attempts=data["max_attempts"],
        time_limit_seconds=data["time_limit_seconds"],
        round_duration_seconds=data["round_duration_seconds"],
        closes_at=(datetime.fromisoformat(data["round_closes_at"])
                   if data.get("round_closes_at") else None),
        prize_first=data["prize_first"],
        prize_second=data["prize_second"],
        prize_third=data["prize_third"],
        prize_consolation=data["prize_consolation"],
        group_id=group_registry.get_group_id() or None,
    )
    round_.status = "ready"
    await db_session.commit()
    # The card BECOMES the confirmation. Left as it was, its «✏️ campo» buttons
    # would still be tappable over a flow that no longer exists.
    await _panel(
        callback.message,
        state,
        f"✅ <b>{esc(spec.label)} #{round_.id} creato!</b>\n\n"
        f"{spec.emoji} {esc(round_.title)}\n"
        f"🏆 {guess_service.format_prize_summary(round_)}\n\n"
        "Avvialo subito nel gruppo oppure programmalo:",
        _created_kb(data["kind"], round_.id),
    )
    await state.clear()
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() is None:
        await callback.answer()
        return
    await _panel(
        callback.message,
        state,
        "⚠️ Sicuro di voler annullare? I dati inseriti andranno persi.",
        confirm_cancel_kb(
            GuessNewCb(action="cancel_yes").pack(), GuessNewCb(action="cancel_no").pack()
        ),
    )
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "cancel_yes"), IsAdminCallbackFilter())
async def cb_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    # Read the medium's id before clearing, then take it off screen with the card:
    # an abandoned flow should leave nothing behind, in the chat or in the DB.
    media_id = (await state.get_data()).get("media_message_id")
    await _panel(callback.message, state, "❌ Creazione annullata.", None)
    await forget_message(callback.message.bot, callback.message.chat.id, media_id)
    await state.clear()
    await callback.answer()


@router.callback_query(GuessNewCb.filter(F.action == "cancel_no"), IsAdminCallbackFilter())
async def cb_cancel_no(callback: CallbackQuery, state: FSMContext) -> None:
    """Back to where they were — not a message saying they are back where they were.

    «Where they were» is the card only once the three mandatory questions are
    answered. Cancelling on question one and then changing your mind used to render
    the card anyway: it reads `title` out of the state data, which isn't there yet,
    and it had already switched the state to `card` — where no message handler
    listens. The flow was unrecoverable except by cancelling for real.
    """
    await _ask_next(callback.message, state, in_panel=True)
    await callback.answer()
