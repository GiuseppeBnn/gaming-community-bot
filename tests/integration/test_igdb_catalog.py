from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_data.config import settings
from database.models import AIGameCatalogEntry
from services import ai_game_service, igdb_catalog
from services.igdb_catalog import CatalogRecord, IGDBCatalogError


def _record(external_id: str, title: str, count: int = 1000) -> CatalogRecord:
    return CatalogRecord(
        external_id=external_id, catalog_key=f"igdb:{external_id}", title=title,
        aliases=(f"{title} alias",), dossier=f"Dossier verificato per {title}. " + "x" * 180,
        source_url=f"https://igdb.com/games/{external_id}", notoriety_count=count,
        source_updated_at=datetime(2026, 1, 1),
    )


@pytest.fixture(autouse=True)
def _minimum(monkeypatch):
    monkeypatch.setattr(settings, "igdb_min_catalog_entries", 1)


async def test_replace_catalog_updates_and_deactivates_in_one_snapshot(session):
    assert await igdb_catalog.replace_catalog(session, (
        _record("1", "Uno"), _record("2", "Due"),
    )) == 2
    await session.commit()

    await igdb_catalog.replace_catalog(session, (_record("1", "Uno aggiornato", 2000),))
    await session.commit()
    rows = list((await session.execute(
        select(AIGameCatalogEntry).order_by(AIGameCatalogEntry.external_id),
    )).scalars())

    assert rows[0].title == "Uno aggiornato" and rows[0].active
    assert rows[0].notoriety_count == 2000
    assert not rows[1].active
    assert json.loads(rows[0].aliases_json) == ["Uno aggiornato alias"]
    assert await igdb_catalog._last_successful_sync(session) is not None


async def test_quality_gate_preserves_previous_catalog(session, monkeypatch):
    monkeypatch.setattr(settings, "igdb_min_catalog_entries", 2)
    with pytest.raises(IGDBCatalogError, match="quality gate"):
        await igdb_catalog.replace_catalog(session, (_record("1", "Uno"),))
    assert not list((await session.execute(select(AIGameCatalogEntry))).scalars())


async def test_game_creation_prefers_active_external_cache_and_snapshots_it(session):
    await igdb_catalog.replace_catalog(session, (
        _record("1", "Uno"), _record("2", "Due"),
    ))
    await session.commit()

    selected = []
    for index in range(2):
        root = await ai_game_service.create_twenty_questions(
            session, creator_tg_id=9, title=f"Partita {index}",
        )
        snapshot = await ai_game_service.get_snapshot(session, root.id)
        selected.append(snapshot.game.catalog_key)
        assert "Dossier verificato" in snapshot.game.dossier_json
        await session.commit()

    assert set(selected) == {"igdb:1", "igdb:2"}


async def test_corrupt_active_cache_fails_closed_instead_of_leaking_bad_context(session):
    session.add(AIGameCatalogEntry(
        game_type="twentyq", source="igdb", external_id="1", catalog_key="igdb:1",
        title="Rotto", aliases_json="not-json", dossier_json="{}",
        notoriety_count=1000, active=True,
    ))
    await session.commit()

    with pytest.raises(RuntimeError, match="corrupt cached"):
        await ai_game_service.create_twenty_questions(
            session, creator_tg_id=9, title="Partita",
        )


async def test_sync_if_due_fetches_then_skips_a_fresh_snapshot(engine, monkeypatch):
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(igdb_catalog, "async_session_maker", factory)
    monkeypatch.setattr(settings, "igdb_client_id", "client")
    monkeypatch.setattr(settings, "igdb_client_secret", "secret")
    calls = 0

    class Client:
        async def fetch_catalog(self):
            nonlocal calls
            calls += 1
            return (_record("1", "Uno"),)

    assert await igdb_catalog.sync_if_due(Client()) == 1
    assert await igdb_catalog.sync_if_due(Client()) is None
    assert calls == 1
