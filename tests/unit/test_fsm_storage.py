"""`_build_storage` deve degradare, non mentire.

`RedisStorage.from_url` costruisce un client senza connettersi: prima di questo
test un Redis irraggiungibile passava il bootstrap indisturbato e si manifestava
molto dopo, come una conversazione che non ricorda niente. STEERING §2 poneva
esattamente questa condizione per passare a Redis — «non riproporre il passaggio
senza prima aggiungere un fallback su errore di connessione».
"""

from __future__ import annotations

import logging

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import main
from config_data.config import settings


class _FakeRedis:
    def __init__(self, *, fails: bool) -> None:
        self._fails = fails
        self.pinged = False

    async def ping(self) -> bool:
        self.pinged = True
        if self._fails:
            raise ConnectionError("Connection refused")
        return True


class _FakeStorage:
    """Sta al posto di RedisStorage: si costruisce senza connettersi, come il vero."""

    def __init__(self, *, fails: bool) -> None:
        self.redis = _FakeRedis(fails=fails)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def redis_storage(monkeypatch):
    """Sostituisce RedisStorage e registra se e come è stato costruito."""

    def _install(*, fails: bool) -> dict:
        made: dict = {}

        class _Factory:
            @staticmethod
            def from_url(url: str, **kwargs) -> _FakeStorage:
                made["url"] = url
                made["kwargs"] = kwargs
                made["storage"] = _FakeStorage(fails=fails)
                return made["storage"]

        monkeypatch.setattr("aiogram.fsm.storage.redis.RedisStorage", _Factory)
        return made

    return _install


async def test_memory_setting_never_builds_a_redis_client(redis_storage, monkeypatch):
    made = redis_storage(fails=False)
    monkeypatch.setattr(settings, "fsm_storage", "memory")

    storage = await main._build_storage()

    assert isinstance(storage, MemoryStorage)
    assert "storage" not in made, "con memory non si tocca Redis nemmeno per costruirlo"


async def test_reachable_redis_is_used(redis_storage, monkeypatch):
    made = redis_storage(fails=False)
    monkeypatch.setattr(settings, "fsm_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://example:6379/0")

    storage = await main._build_storage()

    assert storage is made["storage"]
    assert made["storage"].redis.pinged, "il ping è tutto il punto di questo cambio"
    assert made["url"] == "redis://example:6379/0"


async def test_key_builder_accepts_the_destiny_aiogram_dialog_uses(redis_storage, monkeypatch):
    """Non basta che il kwarg ci sia: deve reggere la chiave che i dialoghi scrivono.

    `aiogram_dialog` salva stack e contesto sotto `StorageKey.destiny`, e il key
    builder di default *solleva* `ValueError` per qualunque destiny diverso da
    "default". Con Redis raggiungibile e `setup_dialogs` cablato, il bot moriva al
    primo messaggio. Qui l'asserzione è sul comportamento, non sul mock: passare
    `with_destiny=False` non la salverebbe.
    """
    made = redis_storage(fails=False)
    monkeypatch.setattr(settings, "fsm_storage", "redis")

    await main._build_storage()

    key_builder = made["kwargs"]["key_builder"]
    built = key_builder.build(StorageKey(bot_id=1, chat_id=2, user_id=3, destiny="aiogd:stack:"))
    assert built.endswith("aiogd:stack:")


async def test_unreachable_redis_degrades_to_memory(redis_storage, monkeypatch, caplog):
    made = redis_storage(fails=True)
    monkeypatch.setattr(settings, "fsm_storage", "redis")

    with caplog.at_level(logging.WARNING):
        storage = await main._build_storage()

    assert isinstance(storage, MemoryStorage), "un Redis morto non deve impedire l'avvio"
    assert made["storage"].closed, "il client a metà non deve restare appeso"
    assert "Redis" in caplog.text
