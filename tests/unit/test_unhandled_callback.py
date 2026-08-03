"""Una callback che nessuno gestisce riceve comunque una risposta.

`common.router` è l'ultimo (`handlers/__init__.py`), e prima di questo handler non
aveva **nessun** handler di callback: un bottone di una tastiera più vecchia del
deploy corrente non produceva niente e la rotellina restava a girare finché
Telegram non mollava.

Due cose atterrano qui, e vanno distinte: un bottone vecchio — normale, l'utente
merita una risposta — e un handler che ha smesso di fare match per sbaglio, che
senza un log resterebbe muto per sempre.
"""

from __future__ import annotations

import logging

from handlers import common


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def test_unhandled_callback_gets_an_answer():
    callback = _FakeCallback("sched:cancel")

    await common.cb_unhandled(callback)

    assert callback.answers, "senza risposta la rotellina resta a girare"
    text, _alert = callback.answers[0]
    assert text == common._UNHANDLED_CALLBACK


async def test_unhandled_callback_is_logged_for_the_admins(caplog):
    callback = _FakeCallback("ev:list:quiz")

    with caplog.at_level(logging.WARNING):
        await common.cb_unhandled(callback)

    record = next(r for r in caplog.records if "Callback non gestita" in r.getMessage())
    assert "ev:list:quiz" in record.getMessage()
    assert record.msg == "Callback non gestita: %s", (
        "il payload deve restare un argomento: utils.alerts deduplica sul template, "
        "e una f-string trasformerebbe ogni click su un bottone vecchio in un alert nuovo"
    )
