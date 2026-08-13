from __future__ import annotations

import re
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import aiohttp
from aioresponses import aioresponses

from config_data.config import settings
from services import igdb_catalog
from services.igdb_catalog import CatalogRecord, IGDBCatalogError, IGDBClient

_TOKEN_REQUEST = re.compile(r"^https://id\.twitch\.tv/oauth2/token\?")


def _row(**overrides):
    value = {
        "id": 1942,
        "name": "Portal 2",
        "summary": "A" * 200,
        "storyline": "Chell torna nei laboratori Aperture Science.",
        "first_release_date": 1_303_084_800,
        "updated_at": 1_700_000_000,
        "total_rating_count": 5000,
        "url": "https://www.igdb.com/games/portal-2",
        "alternative_names": [{"name": "Portal Two"}, {"name": "Portal 2"}],
        "genres": [{"name": "Puzzle"}],
        "themes": [{"name": "Science fiction"}],
        "game_modes": [{"name": "Single player"}, {"name": "Co-operative"}],
        "player_perspectives": [{"name": "First person"}],
        "involved_companies": [
            {"developer": True, "company": {"name": "Valve"}},
            {"developer": False, "company": {"name": "Publisher"}},
        ],
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "igdb_client_id", "client")
    monkeypatch.setattr(settings, "igdb_client_secret", "secret")
    monkeypatch.setattr(settings, "igdb_catalog_size", 2)
    monkeypatch.setattr(settings, "igdb_min_rating_count", 100)
    monkeypatch.setattr(settings, "igdb_min_catalog_entries", 1)
    monkeypatch.setattr(settings, "igdb_timeout_seconds", 5)
    monkeypatch.setattr(settings, "igdb_sync_interval_hours", 24)


def test_normalize_game_builds_a_bounded_playable_dossier():
    record = igdb_catalog.normalize_game(_row())

    assert record is not None
    assert record.catalog_key == "igdb:1942"
    assert record.aliases == ("Portal Two",)
    assert "Puzzle" in record.dossier
    assert "First person" in record.dossier
    assert "Valve" in record.dossier
    assert record.source_updated_at == datetime(2023, 11, 14, 22, 13, 20)
    assert igdb_catalog.normalize_game(None) is None
    assert igdb_catalog._developer_names("not-a-list") == []


@pytest.mark.parametrize("overrides", [
    {"id": None},
    {"name": ""},
    {"summary": "corta"},
    {"total_rating_count": 99},
    {"first_release_date": None},
    {"first_release_date": 9_999_999_999},
])
def test_normalize_game_rejects_incomplete_obscure_or_unreleased_rows(overrides):
    assert igdb_catalog.normalize_game(_row(**overrides)) is None


def test_normalize_tolerates_bad_optional_shapes_and_bounds_alias_storage():
    aliases = [{"name": f"Alias {index} " + "x" * 90} for index in range(100)]
    record = igdb_catalog.normalize_game(_row(
        alternative_names=aliases, genres="bad", themes=[None],
        involved_companies=[None, {"developer": True, "company": None}],
        updated_at=10**30, url=7,
    ))

    assert record is not None
    assert len(str(record.aliases)) < 2300
    assert record.source_updated_at is None
    assert record.source_url is None


async def test_client_authenticates_pages_and_reuses_the_token(monkeypatch):
    monkeypatch.setattr(settings, "igdb_catalog_size", 2)
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, payload={
            "access_token": "token", "expires_in": 3600,
        })
        mocked.post(igdb_catalog._GAMES_URL, payload=[_row(), _row(id=2, name="Doom")])

        records = await IGDBClient().fetch_catalog()

    assert {record.external_id for record in records} == {"1942", "2"}
    game_request = next(
        calls[0] for (method, url), calls in mocked.requests.items()
        if str(url) == igdb_catalog._GAMES_URL
    )
    assert game_request.kwargs["headers"]["Authorization"] == "Bearer token"
    assert "game_type = 0" in game_request.kwargs["data"]
    assert "total_rating_count >= 100" in game_request.kwargs["data"]


async def test_client_refreshes_an_unauthorized_token():
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, payload={"access_token": "old", "expires_in": 3600})
        mocked.post(igdb_catalog._GAMES_URL, status=401)
        mocked.post(_TOKEN_REQUEST, payload={"access_token": "new", "expires_in": 3600})
        mocked.post(igdb_catalog._GAMES_URL, payload=[_row(), _row(id=2, name="Doom")])

        assert len(await IGDBClient().fetch_catalog()) == 2


async def test_client_retries_transient_failures(monkeypatch):
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(igdb_catalog.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, payload={"access_token": "token", "expires_in": 3600})
        mocked.post(igdb_catalog._GAMES_URL, status=429, headers={"Retry-After": "invalid"})
        mocked.post(igdb_catalog._GAMES_URL, payload=[_row(), _row(id=2, name="Doom")])

        assert len(await IGDBClient().fetch_catalog()) == 2
    assert sleeps == [1.0]


async def test_oauth_retries_a_transient_status(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(igdb_catalog.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, status=503)
        mocked.post(_TOKEN_REQUEST, payload={"access_token": "token", "expires_in": 3600})
        mocked.post(igdb_catalog._GAMES_URL, payload=[])

        assert await IGDBClient().fetch_catalog() == ()


@pytest.mark.parametrize("status,payload,match", [
    (403, None, "oauth status"),
    (200, {}, "malformed oauth"),
    (200, {"access_token": "", "expires_in": 10}, "empty oauth"),
])
async def test_oauth_failures_are_normalized(status, payload, match):
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, status=status, payload=payload)
        with pytest.raises(IGDBCatalogError, match=match):
            await IGDBClient().fetch_catalog()


async def test_bad_games_response_and_permanent_status_are_normalized():
    for response, match in (({"payload": {}}, "malformed games"), ({"status": 400}, "status 400")):
        with aioresponses() as mocked:
            mocked.post(_TOKEN_REQUEST, payload={
                "access_token": "token", "expires_in": 3600,
            })
            mocked.post(igdb_catalog._GAMES_URL, **response)
            with pytest.raises(IGDBCatalogError, match=match):
                await IGDBClient().fetch_catalog()


async def test_transport_failures_retry_then_return_a_safe_error(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(igdb_catalog.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, exception=aiohttp.ClientConnectionError("secret URL"))
        mocked.post(_TOKEN_REQUEST, exception=aiohttp.ClientConnectionError("secret URL"))
        with pytest.raises(IGDBCatalogError, match="oauth network") as raised:
            await IGDBClient().fetch_catalog()
    assert raised.value.__cause__ is None

    with aioresponses() as mocked:
        mocked.post(_TOKEN_REQUEST, payload={"access_token": "token", "expires_in": 3600})
        mocked.post(igdb_catalog._GAMES_URL, exception=aiohttp.ClientConnectionError("down"))
        mocked.post(igdb_catalog._GAMES_URL, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(IGDBCatalogError, match="games network"):
            await IGDBClient().fetch_catalog()


def test_enabled_and_due_contract(monkeypatch):
    assert igdb_catalog.is_enabled()
    assert igdb_catalog._is_due(None)
    recent = datetime.now(timezone.utc).replace(tzinfo=None)
    assert not igdb_catalog._is_due(recent)
    assert igdb_catalog._is_due(recent - timedelta(hours=25))
    monkeypatch.setattr(settings, "igdb_client_secret", "")
    assert not igdb_catalog.is_enabled()


def test_catalog_record_is_immutable():
    record = CatalogRecord("1", "igdb:1", "A", (), "facts", None, 100, None)
    with pytest.raises(AttributeError):
        record.title = "B"  # type: ignore[misc]


async def test_disabled_sync_does_not_touch_storage(monkeypatch):
    monkeypatch.setattr(settings, "igdb_client_id", "")
    assert await igdb_catalog.sync_if_due() is None


async def test_sync_loop_survives_a_failure(monkeypatch, caplog):
    calls = 0

    async def fail(_client):
        nonlocal calls
        calls += 1
        raise IGDBCatalogError("down")

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(igdb_catalog, "sync_if_due", fail)
    monkeypatch.setattr(igdb_catalog.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await igdb_catalog.catalog_sync_loop()

    assert calls == 1
    assert "mantengo la cache" in caplog.text


async def test_sync_loop_logs_a_success(monkeypatch, caplog):
    async def succeed(_client):
        return 300

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(igdb_catalog, "sync_if_due", succeed)
    monkeypatch.setattr(igdb_catalog.asyncio, "sleep", stop)
    with caplog.at_level("INFO"):
        with pytest.raises(asyncio.CancelledError):
            await igdb_catalog.catalog_sync_loop()

    assert "300 giochi qualificati" in caplog.text
