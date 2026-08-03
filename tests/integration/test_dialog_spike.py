"""La fetta verticale: aiogram-dialog deve convivere con lo stack che c'è già.

Non prova una schermata del prodotto. Prova le quattro cose che, se non
reggessero, renderebbero inutile riscrivere qualunque flusso: che un dialogo
parta da un comando, che il filtro admin lo protegga, che la sessione DB arrivi
nel getter con la chiave `db_session` di §4, e che il testo mostrato venga dal
getter e non da una costante.

`BotClient` manda update veri attraverso il dispatcher reale, middleware
compresi — è un impianto più fedele di quello a stub usato altrove, non meno.
"""

from __future__ import annotations

import pytest
from aiogram import Dispatcher
from aiogram_dialog import setup_dialogs
from aiogram_dialog.test_tools import BotClient, MockMessageManager

from handlers import dialog_spike


@pytest.fixture
def dp(session, monkeypatch):
    """Un dispatcher col solo router dello spike e la sessione dei test."""
    async def _fake_session_middleware(handler, event, data):
        data["db_session"] = session
        return await handler(event, data)

    dispatcher = Dispatcher()
    dispatcher.update.middleware(_fake_session_middleware)
    # `dialog_spike.router` is a module-level singleton — same object
    # `handlers.ROUTERS` holds — and aiogram's `Router.parent_router` setter
    # permanently refuses a second parent (see the comment on
    # `TestRegister.test_register_attaches_in_declared_order` in
    # `tests/unit/test_router_order.py`, which sidesteps this by using
    # throwaway routers instead of the real ones). This fixture is
    # function-scoped and builds a fresh `Dispatcher` per test, so without
    # detaching first, only the first test using `dp` would pass — every
    # later one would hit `RuntimeError: Router is already attached to ...`.
    dialog_spike.router._parent_router = None
    dispatcher.include_router(dialog_spike.router)
    yield dispatcher
    # Leave the singleton detached: a future test file that also builds a real
    # `Dispatcher` and attaches `dialog_spike.router` without doing its own
    # reset must not depend on collection order to avoid the same `RuntimeError`.
    dialog_spike.router._parent_router = None


@pytest.fixture
def message_manager():
    return MockMessageManager()


@pytest.fixture
def admin_env(monkeypatch):
    """Rende admin l'utente 1 **attraverso il codice vero**.

    `admin_filter.is_admin` controlla `settings.admin_ids` per primo, quindi
    impostare quella lista esercita la policy invece di sostituirla. È lo stesso
    approccio di `tests/unit/test_admin_routers_gated.py`, che esiste apposta
    perché non nascano due modi divergenti di simulare un admin.
    """
    from filters import admin_filter as af

    monkeypatch.setattr(af.settings, "admin_ids", [1])


async def test_an_admin_gets_the_dialog_and_the_getter_sees_the_db(
    dp, message_manager, session, user_factory, admin_env
):
    await user_factory(tg_id=1, full_name="Giupeppe")
    setup_dialogs(dp, message_manager=message_manager)
    client = BotClient(dp, user_id=1, chat_id=1)

    await client.send("/spike")

    message_manager.assert_one_message()
    assert "Utenti a DB: 1" in message_manager.last_message().text, (
        "il getter deve mostrare un conteggio letto dal DB, non una costante: "
        "è questo che dimostra che db_session è arrivato dal middleware"
    )


async def test_a_non_admin_gets_nothing(
    dp, message_manager, session, user_factory, admin_env
):
    """Il filtro alla radice deve valere anche per l'ingresso di un dialogo.

    Se fallisse, ogni dialogo admin del futuro sarebbe aperto a chiunque —
    e lo stato del dialogo, come quello FSM, non ha TTL.
    """
    await user_factory(tg_id=2, full_name="Estraneo")
    setup_dialogs(dp, message_manager=message_manager)
    client = BotClient(dp, user_id=2, chat_id=2)

    await client.send("/spike")

    assert not message_manager.sent_messages, (
        "un non-admin non deve nemmeno vedere la prima finestra"
    )
