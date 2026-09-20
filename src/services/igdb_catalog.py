"""IGDB-backed catalog cache for 20 Domande.

IGDB is contacted only by the background synchronizer. Game creation reads the
normalized local snapshot, so an upstream outage never delays or breaks a game.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.connection import async_session_maker
from database.models import AIGameCatalogEntry

log = logging.getLogger(__name__)

SOURCE = "igdb"
GAME_TYPE = "twentyq"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_GAMES_URL = "https://api.igdb.com/v4/games"
_PAGE_SIZE = 500
_CHECK_INTERVAL_SECONDS = 1800
_MIN_SUMMARY_CHARS = 160
_MAX_DOSSIER_CHARS = 8000
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_SPACE = re.compile(r"\s+")


class IGDBCatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    external_id: str
    catalog_key: str
    title: str
    aliases: tuple[str, ...]
    dossier: str
    source_url: str | None
    notoriety_count: int
    source_updated_at: datetime | None


def is_enabled() -> bool:
    return bool(settings.igdb_client_id and settings.igdb_client_secret)


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _SPACE.sub(" ", value.replace("\x00", " ")).strip()[:limit]


def _related_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names = [_clean(item.get("name"), 100) for item in value if isinstance(item, dict)]
    return [name for name in names if name]


def _developer_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("developer"):
            continue
        company = item.get("company")
        if isinstance(company, dict) and (name := _clean(company.get("name"), 100)):
            names.append(name)
    return names


def _compact_aliases(title: str, value: object) -> tuple[str, ...]:
    names = _related_names(value)
    result: list[str] = []
    seen = {title.casefold()}
    for name in names:
        folded = name.casefold()
        if folded in seen:
            continue
        candidate = [*result, name]
        if len(json.dumps(candidate, ensure_ascii=False)) > 2000:
            break
        seen.add(folded)
        result.append(name)
    return tuple(result)


def normalize_game(value: object) -> CatalogRecord | None:
    """Turn an IGDB row into a bounded dossier, rejecting weak/obscure rows."""
    if not isinstance(value, dict):
        return None
    external_id = value.get("id")
    title = _clean(value.get("name"), 200)
    summary = _clean(value.get("summary"), 4000)
    count = value.get("total_rating_count")
    released = value.get("first_release_date")
    if (
        not isinstance(external_id, int) or external_id <= 0
        or not title or len(summary) < _MIN_SUMMARY_CHARS
        or not isinstance(count, int) or count < settings.igdb_min_rating_count
        or not isinstance(released, int)
    ):
        return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if released <= 0 or released > now_ts:
        return None

    try:
        release_year = datetime.fromtimestamp(released, tz=timezone.utc).year
    except (OverflowError, OSError, ValueError):
        return None
    sections = [f"Descrizione: {summary}", f"Prima pubblicazione: {release_year}."]
    for label, names in (
        ("Generi", _related_names(value.get("genres"))),
        ("Temi", _related_names(value.get("themes"))),
        ("Modalità", _related_names(value.get("game_modes"))),
        ("Prospettiva", _related_names(value.get("player_perspectives"))),
        ("Sviluppatori", _developer_names(value.get("involved_companies"))),
    ):
        if names:
            sections.append(f"{label}: {', '.join(names)}.")
    if storyline := _clean(value.get("storyline"), 3000):
        sections.append(f"Trama: {storyline}")
    dossier = "\n".join(sections)[:_MAX_DOSSIER_CHARS]

    source_updated_at = None
    if isinstance(value.get("updated_at"), int):
        try:
            source_updated_at = datetime.fromtimestamp(value["updated_at"], tz=timezone.utc)
            source_updated_at = source_updated_at.replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            pass
    url = _clean(value.get("url"), 512) or None
    return CatalogRecord(
        external_id=str(external_id), catalog_key=f"igdb:{external_id}", title=title,
        aliases=_compact_aliases(title, value.get("alternative_names")), dossier=dossier,
        source_url=url, notoriety_count=count, source_updated_at=source_updated_at,
    )


class IGDBClient:
    """Small server-side OAuth client with in-memory token reuse and one retry."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def _get_token(self, client: aiohttp.ClientSession) -> str:
        loop_time = asyncio.get_running_loop().time()
        if self._token and loop_time < self._token_expires_at:
            return self._token
        for attempt in (1, 2):
            try:
                async with client.post(_TOKEN_URL, params={
                    "client_id": settings.igdb_client_id,
                    "client_secret": settings.igdb_client_secret,
                    "grant_type": "client_credentials",
                }) as response:
                    if response.status == 200:
                        try:
                            payload = await response.json(content_type=None)
                            token = payload["access_token"]
                            expires_in = int(payload["expires_in"])
                        except (KeyError, TypeError, ValueError) as exc:
                            raise IGDBCatalogError("malformed oauth response") from exc
                        if not isinstance(token, str) or not token:
                            raise IGDBCatalogError("empty oauth token")
                        self._token = token
                        self._token_expires_at = loop_time + max(0, expires_in - 60)
                        return token
                    if response.status not in _RETRYABLE or attempt == 2:
                        raise IGDBCatalogError(f"oauth status {response.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    # Do not chain aiohttp's request URL: OAuth credentials are
                    # query parameters in Twitch's documented flow.
                    raise IGDBCatalogError("oauth network error") from None
            await asyncio.sleep(0.5)
        raise IGDBCatalogError("oauth request failed")

    async def _games_page(
        self, client: aiohttp.ClientSession, *, offset: int, limit: int,
    ) -> list[dict[str, Any]]:
        query = (
            "fields id,name,url,first_release_date,updated_at,total_rating_count,summary,"
            "storyline,alternative_names.name,genres.name,themes.name,game_modes.name,"
            "player_perspectives.name,involved_companies.developer,"
            "involved_companies.company.name; "
            f"where game_type = 0 & version_parent = null & parent_game = null & "
            f"first_release_date != null & first_release_date <= "
            f"{int(datetime.now(timezone.utc).timestamp())} & summary != null & "
            f"total_rating_count >= {settings.igdb_min_rating_count}; "
            f"sort total_rating_count desc; limit {limit}; offset {offset};"
        )
        for attempt in (1, 2):
            try:
                token = await self._get_token(client)
                async with client.post(_GAMES_URL, headers={
                    "Client-ID": settings.igdb_client_id,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                }, data=query) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        if not isinstance(payload, list) or not all(
                            isinstance(item, dict) for item in payload
                        ):
                            raise IGDBCatalogError("malformed games response")
                        return payload
                    if response.status == 401 and attempt == 1:
                        self._token = None
                        continue
                    if response.status in _RETRYABLE and attempt == 1:
                        retry_after = response.headers.get("Retry-After", "1")
                        try:
                            delay = min(5.0, max(0.1, float(retry_after)))
                        except ValueError:
                            delay = 1.0
                        await asyncio.sleep(delay)
                        continue
                    raise IGDBCatalogError(f"games status {response.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    raise IGDBCatalogError("games network error") from None
                await asyncio.sleep(0.5)
        raise IGDBCatalogError("games request failed")

    async def fetch_catalog(self) -> tuple[CatalogRecord, ...]:
        timeout = aiohttp.ClientTimeout(total=settings.igdb_timeout_seconds)
        records: list[CatalogRecord] = []
        offset = 0
        async with aiohttp.ClientSession(timeout=timeout) as client:
            while len(records) < settings.igdb_catalog_size:
                limit = min(_PAGE_SIZE, settings.igdb_catalog_size - len(records))
                page = await self._games_page(client, offset=offset, limit=limit)
                offset += len(page)
                records.extend(record for row in page if (record := normalize_game(row)))
                if len(page) < limit:
                    break
        # IDs should be unique across pages, but dedup here makes an unstable
        # upstream page boundary harmless.
        unique = {record.external_id: record for record in records}
        return tuple(unique.values())


async def replace_catalog(session: AsyncSession, records: tuple[CatalogRecord, ...]) -> int:
    """Atomically replace the active IGDB set. Caller owns commit/rollback."""
    if len(records) < settings.igdb_min_catalog_entries:
        raise IGDBCatalogError(
            f"quality gate returned only {len(records)} entries "
            f"(< {settings.igdb_min_catalog_entries})"
        )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = list((await session.execute(select(AIGameCatalogEntry).where(
        AIGameCatalogEntry.game_type == GAME_TYPE,
        AIGameCatalogEntry.source == SOURCE,
    ))).scalars())
    by_external_id = {entry.external_id: entry for entry in existing}
    seen: set[str] = set()
    for record in records:
        seen.add(record.external_id)
        entry = by_external_id.get(record.external_id)
        if entry is None:
            entry = AIGameCatalogEntry(
                game_type=GAME_TYPE, source=SOURCE, external_id=record.external_id,
                catalog_key=record.catalog_key, title=record.title, aliases_json="[]",
                dossier_json="{}", notoriety_count=record.notoriety_count,
            )
            session.add(entry)
        entry.catalog_key = record.catalog_key
        entry.title = record.title
        entry.aliases_json = json.dumps(record.aliases, ensure_ascii=False)
        entry.dossier_json = json.dumps({"facts": record.dossier}, ensure_ascii=False)
        entry.source_url = record.source_url
        entry.notoriety_count = record.notoriety_count
        entry.source_updated_at = record.source_updated_at
        entry.active = True
        entry.synced_at = now
    for entry in existing:
        if entry.external_id not in seen:
            entry.active = False
            entry.synced_at = now
    await session.flush()
    return len(records)


async def _last_successful_sync(session: AsyncSession) -> datetime | None:
    return (await session.execute(select(func.max(AIGameCatalogEntry.synced_at)).where(
        AIGameCatalogEntry.game_type == GAME_TYPE,
        AIGameCatalogEntry.source == SOURCE,
    ))).scalar_one_or_none()


def _is_due(last_sync: datetime | None) -> bool:
    if last_sync is None:
        return True
    return datetime.now(timezone.utc).replace(tzinfo=None) - last_sync >= timedelta(
        hours=settings.igdb_sync_interval_hours,
    )


async def sync_if_due(client: IGDBClient | None = None) -> int | None:
    """Fetch without a DB transaction, then publish the new snapshot atomically."""
    if not is_enabled():
        return None
    async with async_session_maker() as session:
        due = _is_due(await _last_successful_sync(session))
    if not due:
        return None

    records = await (client or IGDBClient()).fetch_catalog()
    async with async_session_maker() as session:
        count = await replace_catalog(session, records)
        await session.commit()
    return count


async def catalog_sync_loop() -> None:
    state = "attivo" if is_enabled() else "disattivo (credenziali assenti)"
    log.info("Sync catalogo IGDB %s.", state)
    client = IGDBClient()
    while True:
        try:
            count = await sync_if_due(client)
            if count is not None:
                log.info("Catalogo IGDB aggiornato: %d giochi qualificati.", count)
        except Exception:  # noqa: BLE001 — cached/fallback games keep the bot playable
            log.exception("Sync catalogo IGDB fallito; mantengo la cache precedente.")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
