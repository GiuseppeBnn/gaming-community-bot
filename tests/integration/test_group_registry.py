"""Integration tests for services/group_registry — the runtime effective group id.

Covers the fix for the "public/private toggle breaks admin perms" bug: a chat
migration must update (and persist) the effective group id, survive a restart,
and be discarded if the operator later changes GROUP_ID in the .env.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from config_data.config import settings
from database.models import BotState
from services import group_registry


@pytest.fixture(autouse=True)
def _reset_effective():
    """Isolate the module-level in-memory override between tests."""
    group_registry.set_runtime_group_id(None)
    yield
    group_registry.set_runtime_group_id(None)


def test_get_group_id_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(settings, "group_id", -100123)
    assert group_registry.get_group_id() == -100123


def test_set_runtime_overrides_settings(monkeypatch):
    monkeypatch.setattr(settings, "group_id", -100123)
    group_registry.set_runtime_group_id(-100999)
    assert group_registry.get_group_id() == -100999


async def test_record_migration_persists_and_survives_restart(session, monkeypatch):
    monkeypatch.setattr(settings, "group_id", -100100)
    await group_registry.record_migration(session, -100100, -100200)
    await session.commit()
    assert group_registry.get_group_id() == -100200

    # Simulate a restart: drop in-memory state, reload from the DB.
    group_registry.set_runtime_group_id(None)
    effective = await group_registry.load(session)
    assert effective == -100200
    assert group_registry.get_group_id() == -100200


async def test_load_discards_override_when_env_changed(session, monkeypatch):
    monkeypatch.setattr(settings, "group_id", -100100)
    await group_registry.record_migration(session, -100100, -100200)
    await session.commit()

    # Operator edited GROUP_ID in the .env after the migration was recorded.
    monkeypatch.setattr(settings, "group_id", -100777)
    group_registry.set_runtime_group_id(None)
    effective = await group_registry.load(session)

    assert effective == -100777  # trust the new .env, not the stale override
    rows = (await session.execute(select(BotState))).scalars().all()
    assert rows == []  # stale bot_state rows cleaned up


async def test_load_without_override_returns_settings(session, monkeypatch):
    monkeypatch.setattr(settings, "group_id", -100555)
    effective = await group_registry.load(session)
    assert effective == -100555
