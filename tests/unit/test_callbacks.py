"""I payload delle callback, tipizzati.

Prima di questo modulo ogni schermata inventava la sua grammatica e la
ri-parsava a mano: `_, _, task_type, raw = callback.data.split(":")`, ripetuto,
con guardie `isdigit()` sparse e il limite dei 64 byte rispettato a occhio.

Qui si pinnano le tre cose che il parsing a mano non garantiva: che il payload
prodotto sia quello atteso, che un payload malformato **non arrivi** all'handler,
e che i limiti di Telegram si presentino come errori in test invece che come
bottoni rotti in chat.
"""

from __future__ import annotations

import pytest
from aiogram.types import CallbackQuery, User

from handlers.callbacks import SchedCb


def _query(data: str) -> CallbackQuery:
    """Una CallbackQuery vera: il filtro fa `isinstance`, un finto darebbe
    `False` per il motivo sbagliato."""
    return CallbackQuery(
        id="1", from_user=User(id=1, is_bot=False, first_name="A"),
        chat_instance="x", data=data,
    )


@pytest.mark.parametrize("cb, expected", [
    (SchedCb(action="cancel"), "sched:cancel::"),
    (SchedCb(action="type", key="quiz"), "sched:type:quiz:"),
    (SchedCb(action="pick", key="quiz", item_id=7), "sched:pick:quiz:7"),
    (SchedCb(action="del", item_id=7), "sched:del::7"),
])
def test_pack(cb, expected):
    assert cb.pack() == expected


def test_unpack_restores_the_types():
    cb = SchedCb.unpack("sched:pick:quiz:7")
    assert cb.action == "pick"
    assert cb.key == "quiz"
    assert cb.item_id == 7, "l'id deve tornare int, non str"


async def test_a_non_numeric_id_never_reaches_the_handler():
    """Oggi `cb_pick_event` si difende con `raw_id.isdigit()`. Domani non arriva."""
    assert await SchedCb.filter()(_query("sched:pick:quiz:abc")) is False


async def test_a_well_formed_payload_is_injected():
    result = await SchedCb.filter()(_query("sched:pick:quiz:7"))
    assert result == {"callback_data": SchedCb(action="pick", key="quiz", item_id=7)}


async def test_a_payload_from_an_older_deploy_falls_through():
    """I campi opzionali lasciano i separatori: il payload vecchio è più corto.

    Non è un difetto da nascondere — è il motivo per cui il catch-all di
    `common` (Task 1) esiste e va per primo.
    """
    assert await SchedCb.filter()(_query("sched:cancel")) is False


def test_the_separator_cannot_hide_in_a_value():
    with pytest.raises(ValueError, match="Separator symbol"):
        SchedCb(action="type", key="a:b").pack()


def test_the_64_byte_ceiling_shows_up_in_tests_not_in_chat():
    with pytest.raises(ValueError, match="too long"):
        SchedCb(action="x" * 70).pack()
