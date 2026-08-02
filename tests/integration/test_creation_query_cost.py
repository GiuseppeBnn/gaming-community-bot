"""Quanto costa, in query, creare un round dall'inizio alla pubblicazione.

Serve a due cose. La prima è il confronto: aiogram-dialog sposta i dati dallo
stato FSM ai `getter`, che girano a ogni ridisegno, e senza il numero di
partenza «costa di più» resterebbe un'impressione. La seconda vale comunque,
anche se lo spike venisse abbandonato: un conteggio pinnato è la guardia contro
le N+1, che è precisamente il difetto che non si vede finché il DB non è grande.

Il numero atteso è basso per costruzione, non per fortuna: tutto il flusso vive
nello stato FSM e il DB si tocca una volta sola, alla pubblicazione.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from handlers.guess import creation as cr
from tests.integration.test_guess_creation_flow import (
    _BOT, _Cb, _Msg, _to_card,
)


@pytest.fixture
def sql_counter(engine):
    """Ogni statement che passa dal cursore, in ordine.

    Si aggancia al `sync_engine` sottostante perché è lì che SQLAlchemy emette
    l'evento anche per un engine async.
    """
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


@pytest.fixture
def state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=999, chat_id=1, user_id=1))


async def test_the_questions_and_the_edits_cost_nothing_at_all(
    state, sql_counter, session
):
    """Il flusso prima della pubblicazione non deve toccare il DB.

    È l'invariante che rende sensato il confronto con i `getter`: se qui
    comparisse anche una sola query, vorrebbe dire che una schermata sta
    leggendo dal DB ciò che ha già in mano.
    """
    _BOT.reset()
    await _to_card(state)

    for field, value in (("title", "Un titolo nuovo"), ("answer", "Quake")):
        await cr.cb_edit(_Cb(f"guess_new:edit:{field}"), state)
        await cr.fsm_edit_value(_Msg(value), state)

    assert sql_counter == [], (
        f"il flusso pre-pubblicazione ha toccato il DB {len(sql_counter)} volte: "
        f"{sql_counter}"
    )


async def test_publishing_costs_a_known_number_of_statements(
    state, sql_counter, session
):
    """Pinna il costo della pubblicazione.

    Non si asserisce un numero esatto — un `INSERT` in più per una colonna nuova
    è un cambio legittimo — ma un tetto: se il costo raddoppia, qualcuno ha
    introdotto una lettura per riga e questo test è il posto in cui accorgersene.

    Misurato il 2026-08-02: **2 statement** — un `INSERT INTO guess_rounds`
    (il round nasce `draft`, dentro `guess_service.create_round`) seguito da un
    `UPDATE guess_rounds SET status=?` (l'armamento a `ready` in `cb_publish`).
    Il tetto resta a 12, il valore proposto dal brief: è già ampiamente sopra il
    misurato, non abbassato a un numero comodo scelto dopo il fatto.
    """
    _BOT.reset()
    await _to_card(state)
    sql_counter.clear()

    await cr.cb_publish(_Cb("guess_new:publish"), state, session)

    assert sql_counter, "la pubblicazione deve scrivere qualcosa"
    assert len(sql_counter) <= 12, (
        f"la pubblicazione costa {len(sql_counter)} statement, erano ≤12: "
        + "\n".join(sql_counter)
    )
