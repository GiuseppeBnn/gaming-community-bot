"""Integration: total-state export → import roundtrip preserves every value."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import (
    AIFeatureBudgetPeriod,
    AIGameProviderAttempt,
    AIGameRewardAllocation,
    AIGameRewardSettlement,
    AIGameSession,
    AlduinoTurn,
    Base,
    LedgerEntry,
    User,
    Wallet,
)
from services.backup import state_export
from services.backup.state_export import StateExportError


async def _fresh_db():
    """A second, independent in-memory DB to import into (the migration target)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return engine, factory


async def test_roundtrip_preserves_values(tmp_path, session, user_factory):
    await user_factory(111, username="alice", coins=12_345_678_900, xp=999, daily_streak=7)
    session.add(LedgerEntry(to_tg_id=111, amount=12_345_678_900, tx_type="deposit", description="seed"))
    await session.commit()

    snapshot = await state_export.export_state(session, tmp_path)
    assert snapshot.exists()
    assert snapshot.with_name(snapshot.name + ".sha256").exists()
    assert (tmp_path / "state-latest.jsonl.gz").exists()

    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as s2:
            report = await state_export.import_state(s2, snapshot, mode="empty")
            await s2.commit()

            assert report.tables.get("users") == 1
            assert report.tables.get("wallets") == 1
            assert report.tables.get("ledger") == 1

            user = (await s2.execute(select(User).where(User.tg_id == 111))).scalar_one()
            wallet = (await s2.execute(select(Wallet).where(Wallet.tg_id == 111))).scalar_one()
            assert user.username == "alice"
            assert user.xp == 999
            assert user.daily_streak == 7
            assert wallet.coins == 12_345_678_900  # BigInteger survives the roundtrip

            ledger = (await s2.execute(select(LedgerEntry))).scalars().all()
            assert len(ledger) == 1
            assert ledger[0].amount == 12_345_678_900
    finally:
        await engine2.dispose()


async def test_roundtrip_preserves_alduino_reply_branches(tmp_path, session):
    root = AlduinoTurn(
        id=10, group_id=-100, user_tg_id=1, user_message_id=100,
        bot_message_id=101, parent_turn_id=None, input_text="ciao",
        output_text="salve", history_json='[{"user":"ciao","alduino":"salve"}]',
        provider="gemini", provider_interaction_id="interaction-root",
    )
    child = AlduinoTurn(
        id=20, group_id=-100, user_tg_id=2, user_message_id=102,
        bot_message_id=103, parent_turn_id=10, input_text="perché?",
        output_text="per questo", history_json="[]", provider="groq",
        provider_interaction_id=None,
    )
    session.add_all((root, child))
    await session.commit()

    snapshot = await state_export.export_state(session, tmp_path)
    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as target:
            report = await state_export.import_state(target, snapshot)
            await target.commit()
            restored = list((await target.execute(
                select(AlduinoTurn).order_by(AlduinoTurn.id)
            )).scalars())

        assert report.tables["alduino_turns"] == 2
        assert restored[1].parent_turn_id == restored[0].id == 10
        assert restored[0].provider_interaction_id == "interaction-root"
        assert restored[1].provider == "groq"
    finally:
        await engine2.dispose()


async def test_roundtrip_preserves_v2_reward_and_provider_audits(tmp_path, session, user_factory):
    await user_factory(111, username="alice", coins=0)
    game = AIGameSession(game_type="twentyq", title="Reward audit", creator_tg_id=111)
    session.add(game)
    await session.flush()
    settlement = AIGameRewardSettlement(
        session_id=game.id,
        policy_version=2,
        max_coins_per_participant=100,
        minimum_bps=3000,
        question_penalty_bps=600,
        wrong_guess_penalty_bps=2000,
        xp_per_participant=10,
        status="settled",
        finish_reason="victory",
        participant_count=1,
        question_count=3,
        wrong_guess_count=1,
        base_amount=100,
        penalty_amount=38,
        computed_pool=62,
        paid_pool=62,
        share=62,
        remainder=0,
    )
    session.add_all(
        (
            settlement,
            AIGameRewardAllocation(session_id=game.id, user_tg_id=111, coins=62, xp=10),
            AIGameProviderAttempt(
                session_id=game.id,
                operation="answer_question",
                provider="openrouter",
                model="openai/gpt-5",
                prompt_version="twentyq-v2",
                schema_version="1",
                outcome="success",
                error_class=None,
                latency_ms=250,
                prompt_tokens=100,
                completion_tokens=20,
                reasoning_tokens=5,
                cached_tokens=0,
                cost_microusd=321,
            ),
            AIFeatureBudgetPeriod(
                period="2026-08",
                feature="twentyq",
                cap_microusd=1_000_000,
                spent_microusd=321,
                reserved_microusd=0,
            ),
        )
    )
    await session.commit()

    snapshot = await state_export.export_state(session, tmp_path)
    ordered_tables = [table.name for table in Base.metadata.sorted_tables]
    assert ordered_tables.index("ai_game_sessions") < ordered_tables.index(
        "ai_game_reward_settlements"
    ) < ordered_tables.index("ai_game_reward_allocations")

    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as target:
            report = await state_export.import_state(target, snapshot)
            await target.commit()
            restored = await target.get(AIGameRewardSettlement, game.id)
            allocation = (await target.execute(select(AIGameRewardAllocation))).scalar_one()
            attempt = (await target.execute(select(AIGameProviderAttempt))).scalar_one()
            budget = await target.get(AIFeatureBudgetPeriod, ("2026-08", "twentyq"))

        assert report.tables["ai_game_reward_settlements"] == 1
        assert report.tables["ai_game_reward_allocations"] == 1
        assert report.tables["ai_game_provider_attempts"] == 1
        assert report.tables["ai_feature_budget_periods"] == 1
        assert (restored.computed_pool, restored.paid_pool, allocation.coins, allocation.xp) == (
            62,
            62,
            62,
            10,
        )
        assert (attempt.provider, attempt.cost_microusd, budget.spent_microusd) == (
            "openrouter",
            321,
            321,
        )
    finally:
        await engine2.dispose()


async def test_import_refuses_nonempty_db(tmp_path, session, user_factory):
    await user_factory(1, coins=5)
    snapshot = await state_export.export_state(session, tmp_path)
    # The session's DB already has rows → 'empty' mode must refuse.
    with pytest.raises(StateExportError):
        await state_export.import_state(session, snapshot, mode="empty")


async def test_import_rejects_corrupt_snapshot(tmp_path, session, user_factory):
    await user_factory(1, coins=5)
    snapshot = await state_export.export_state(session, tmp_path)
    with open(snapshot, "ab") as f:
        f.write(b"tampered")

    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as s2:
            with pytest.raises(StateExportError):
                await state_export.import_state(s2, snapshot, mode="empty")
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# Rotation, publication and the import guards
# ---------------------------------------------------------------------------
#
# The roundtrip above proves the data survives. What follows is everything around
# it: the snapshot that gets published as `latest`, the old ones that get pruned,
# and the refusals on the way back in. A restore is run once, by hand, on a bad
# day — so every refusal here is a refusal to make that day worse.

import gzip
import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Column, Date, DateTime, Integer, Table


class TestPublishAndPrune:
    async def test_the_snapshot_is_published_as_latest(self, tmp_path, session, user_factory):
        """`state-latest.jsonl.gz` is what the restore script reads by default; if
        it did not move, a restore would silently replay an old snapshot."""
        await user_factory(1, username="alice", coins=10)

        snapshot = await state_export.export_state(session, tmp_path)

        latest = state_export.latest_snapshot_path(tmp_path)
        assert latest.exists()
        assert latest.read_bytes() == snapshot.read_bytes()
        assert (tmp_path / (latest.name + ".sha256")).exists()

    async def test_publishing_again_replaces_the_previous_latest(
        self, tmp_path, session, user_factory
    ):
        await user_factory(1, username="alice", coins=10)
        await state_export.export_state(session, tmp_path)
        stale_tmp = tmp_path / f".{state_export._LATEST_NAME}.{os.getpid()}.tmp"
        stale_tmp.write_bytes(b"leftover from a crashed run")

        second = await state_export.export_state(session, tmp_path)

        latest = state_export.latest_snapshot_path(tmp_path)
        assert latest.read_bytes() == second.read_bytes()
        assert not stale_tmp.exists()

    async def test_it_falls_back_to_a_copy_when_hardlinks_are_unavailable(
        self, tmp_path, session, user_factory, monkeypatch
    ):
        """A hardlink costs zero bytes, but it is not available on every
        filesystem — and `latest` has to exist either way."""
        await user_factory(1, username="alice", coins=10)
        copies: list = []

        def _no_link(src, dst):
            raise OSError("cross-device link")

        real_copy = shutil.copyfile

        def _spy_copy(src, dst):
            copies.append((src, dst))
            return real_copy(src, dst)

        monkeypatch.setattr(os, "link", _no_link)
        monkeypatch.setattr(shutil, "copyfile", _spy_copy)

        snapshot = await state_export.export_state(session, tmp_path)

        assert copies, "the copy fallback must be taken"
        assert state_export.latest_snapshot_path(tmp_path).read_bytes() == snapshot.read_bytes()

    def test_old_snapshots_are_pruned_with_their_checksums(self, tmp_path):
        """Unbounded snapshots fill the disk, which eventually is what stops the
        backups — the failure mode this rotation exists to prevent.

        Driven directly rather than by exporting five times: the filename carries a
        one-second timestamp, so several exports in the same second collapse onto
        one name and the pruning would never actually run.
        """
        for day in range(1, 6):
            snap = tmp_path / f"state-2026070{day}-120000.jsonl.gz"
            snap.write_bytes(b"finto")
            snap.with_name(snap.name + ".sha256").write_text("deadbeef\n")
        latest = tmp_path / state_export._LATEST_NAME
        latest.write_bytes(b"finto")

        state_export._prune(tmp_path, keep=2)

        kept = sorted(p.name for p in tmp_path.glob("state-*.jsonl.gz")
                      if p.name != state_export._LATEST_NAME)
        assert kept == ["state-20260704-120000.jsonl.gz", "state-20260705-120000.jsonl.gz"]
        assert not (tmp_path / "state-20260701-120000.jsonl.gz.sha256").exists()
        assert latest.exists(), "the published latest must never be pruned"

    async def test_the_rotation_is_wired_into_the_export(
        self, tmp_path, session, user_factory
    ):
        await user_factory(1, username="alice", coins=10)
        for day in range(1, 4):
            (tmp_path / f"state-2020010{day}-120000.jsonl.gz").write_bytes(b"vecchio")

        await state_export.export_state(session, tmp_path, keep=1)

        assert not (tmp_path / "state-20200101-120000.jsonl.gz").exists()

    async def test_a_failed_export_leaves_no_temp_file(
        self, tmp_path, session, user_factory, monkeypatch
    ):
        """Otherwise the directory fills with half-written .tmp files that look
        enough like snapshots to be restored by mistake."""
        await user_factory(1, username="alice", coins=10)

        def _boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "replace", _boom)

        with pytest.raises(OSError):
            await state_export.export_state(session, tmp_path)

        assert list(tmp_path.glob("*.tmp")) == []


class TestImportGuards:
    async def _snapshot(self, tmp_path, session, user_factory) -> Path:
        await user_factory(1, username="alice", coins=10)
        return await state_export.export_state(session, tmp_path)

    async def test_a_snapshot_without_a_header_is_refused(self, tmp_path, session):
        """A truncated or hand-edited file must not be half-imported."""
        path = tmp_path / "rotto.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps({"t": "users", "r": {}}) + "\n")

        with pytest.raises(StateExportError, match="Header"):
            await state_export.import_state(session, path)

    async def test_a_snapshot_from_another_schema_version_is_refused(self, tmp_path, session):
        """The columns may have moved; importing anyway would land data in the
        wrong shape and there is no undo."""
        path = tmp_path / "vecchio.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps({"_": "header", "schema_version": 999}) + "\n")

        with pytest.raises(StateExportError, match="schema_version"):
            await state_export.import_state(session, path)

    async def test_an_unknown_mode_is_refused_before_anything_is_touched(
        self, tmp_path, session, user_factory
    ):
        path = await self._snapshot(tmp_path, session, user_factory)

        with pytest.raises(ValueError, match="mode sconosciuto"):
            await state_export.import_state(session, path, mode="qualunque")

    async def test_replace_mode_wipes_the_target_first(
        self, tmp_path, session, user_factory
    ):
        """The destructive path, reserved for the CLI: it must actually clear the
        tables, or the import fails on the first duplicate primary key."""
        path = await self._snapshot(tmp_path, session, user_factory)
        engine, factory = await _fresh_db()
        try:
            async with factory() as target:
                target.add(User(tg_id=1, username="occupato", full_name="Occupato"))
                await target.commit()

                report = await state_export.import_state(target, path, mode="replace")
                await target.commit()

                assert report.total > 0
                name = await target.scalar(select(User.username).where(User.tg_id == 1))
                assert name == "alice"
        finally:
            await engine.dispose()

    async def test_a_snapshot_without_a_checksum_sidecar_is_still_importable(
        self, tmp_path, session, user_factory
    ):
        """Someone copied the .gz off the server without the sidecar. Refusing would
        turn a usable backup into an unusable one."""
        path = await self._snapshot(tmp_path, session, user_factory)
        path.with_name(path.name + ".sha256").unlink()
        engine, factory = await _fresh_db()
        try:
            async with factory() as target:
                report = await state_export.import_state(target, path)
                await target.commit()
                assert report.total > 0
        finally:
            await engine.dispose()

    async def test_rows_of_a_table_this_build_does_not_know_are_skipped(
        self, tmp_path, session, user_factory
    ):
        """A snapshot from a newer build carries tables that do not exist here.
        Skipping them restores what *can* be restored instead of nothing at all."""
        await self._snapshot(tmp_path, session, user_factory)
        path = tmp_path / "misto.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps({"_": "header",
                                 "schema_version": state_export.SCHEMA_VERSION}) + "\n")
            gz.write(json.dumps({"t": "tabella_del_futuro", "r": {"x": 1}}) + "\n")
            gz.write("\n")  # a blank line must not break the parse either
            gz.write(json.dumps({"t": "users",
                                 "r": {"tg_id": 5, "username": "nuovo",
                                       "full_name": "Nuovo"}}) + "\n")

        engine, factory = await _fresh_db()
        try:
            async with factory() as target:
                report = await state_export.import_state(target, path)
                await target.commit()

                assert report.tables == {"users": 1}
                assert report.total == 1
        finally:
            await engine.dispose()

    async def test_more_rows_than_one_batch_are_all_imported(
        self, tmp_path, session, user_factory
    ):
        """The insert is batched; an off-by-one in the flush would drop the tail."""
        rows = state_export._INSERT_BATCH + 7
        for i in range(rows):
            await user_factory(1000 + i, username=f"u{i}", coins=1)
        path = await state_export.export_state(session, tmp_path)

        engine, factory = await _fresh_db()
        try:
            async with factory() as target:
                report = await state_export.import_state(target, path)
                await target.commit()

                assert report.tables["users"] == rows
        finally:
            await engine.dispose()


class TestTemporalColumns:
    def test_both_datetime_and_date_columns_are_recognised(self):
        """A `date` column read back as a string would be inserted as text and then
        compare wrong for the rest of the database's life."""
        from sqlalchemy import MetaData

        table = Table(
            "prova", MetaData(),
            Column("id", Integer, primary_key=True),
            Column("quando", DateTime),
            Column("giorno", Date),
        )

        cols = state_export._temporal_cols(table)

        assert cols == {"quando": datetime, "giorno": date}

    def test_each_kind_is_parsed_into_its_own_type(self):
        assert state_export._parse_temporal(date, "2026-07-27") == date(2026, 7, 27)
        assert state_export._parse_temporal(datetime, "2026-07-27T10:00:00") == \
            datetime(2026, 7, 27, 10, 0)


class TestDialectName:
    async def test_it_reports_the_dialect_in_the_header(self, session):
        assert state_export._dialect_name(session) == "sqlite"

    def test_an_unbindable_session_is_reported_as_unknown(self):
        """Purely informational: it must never be the reason an export fails."""
        class _Broken:
            @property
            def bind(self):
                raise RuntimeError("no bind")

        assert state_export._dialect_name(_Broken()) == "unknown"
