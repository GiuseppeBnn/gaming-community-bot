"""Integration contracts for Alduino's v2 persistent game schema."""

from __future__ import annotations

from sqlalchemy import UniqueConstraint, inspect

from database.models import (
    AIFeatureBudgetPeriod,
    AIGameProviderAttempt,
    AIGameRewardAllocation,
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    Base,
    ScheduledTask,
    TransactionType,
    TwentyQuestionsGame,
)
from services.xp_service import XpSource


def test_v2_schema_and_audit_columns_are_explicit():
    assert TransactionType.ai_game_reward.value == "ai_game_reward"
    assert XpSource.twentyq.value == "twentyq"
    assert AIGameSession.__table__.c.expires_at.nullable
    assert AIGameSession.__table__.c.pending_user_tg_id.nullable
    assert AIGameTurn.__table__.c.normalized_input_hash.type.length == 64
    assert TwentyQuestionsGame.__table__.c.rules_version.default.arg == 1
    assert TwentyQuestionsGame.__table__.c.question_limit.default is None
    assert TwentyQuestionsGame.__table__.c.guess_limit.default is None
    assert ScheduledTask.__table__.c.retry_count.default.arg == 0
    assert AIGameRewardSettlement.__table__.c.session_id.primary_key
    assert {"period", "feature"} == {
        column.name for column in AIFeatureBudgetPeriod.__table__.primary_key.columns
    }
    forbidden = {"prompt", "body", "input_text", "username", "user_tg_id", "group_id"}
    assert forbidden.isdisjoint(AIGameProviderAttempt.__table__.c.keys())


def test_v2_schema_declares_the_audit_tables_and_indexes():
    table_names = {
        "ai_game_reward_settlements",
        "ai_game_reward_allocations",
        "ai_game_provider_attempts",
        "ai_feature_budget_periods",
    }
    assert table_names.issubset(Base.metadata.tables)

    indexes = {
        (index.name, tuple(column.name for column in index.columns), index.unique)
        for index in AIGameTurn.__table__.indexes
    }
    assert ("ix_ai_game_turn_quota", ("session_id", "user_tg_id", "kind"), False) in indexes
    assert (
        "uq_ai_game_turn_normalized",
        ("session_id", "kind", "normalized_input_hash"),
        True,
    ) in indexes

    settlement_fk = next(iter(AIGameRewardSettlement.__table__.c.session_id.foreign_keys))
    allocation_fks = {
        fk.target_fullname: fk.ondelete
        for column in ("session_id", "user_tg_id")
        for fk in AIGameRewardAllocation.__table__.c[column].foreign_keys
    }
    assert settlement_fk.ondelete == "RESTRICT"
    assert allocation_fks == {
        "ai_game_reward_settlements.session_id": "RESTRICT",
        "users.tg_id": "RESTRICT",
    }


async def test_v2_audit_tables_are_created_with_the_test_schema(session):
    connection = await session.connection()
    table_names = await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )

    assert {
        "ai_game_reward_settlements",
        "ai_game_reward_allocations",
        "ai_game_provider_attempts",
        "ai_feature_budget_periods",
    }.issubset(table_names)


def test_allocation_identity_is_unique_per_session_and_user():
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in AIGameRewardAllocation.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("session_id", "user_tg_id") in uniques


async def test_v1_and_v2_games_persist_unambiguous_limit_snapshots(session):
    legacy = AIGameSession(game_type="twentyq", title="Legacy", creator_tg_id=1)
    v2 = AIGameSession(game_type="twentyq", title="V2", creator_tg_id=2)
    session.add_all((legacy, v2))
    await session.flush()
    session.add_all(
        (
            TwentyQuestionsGame(
                session_id=legacy.id,
                catalog_key="legacy",
                answer="Portal",
                aliases_json="[]",
                dossier_json="{}",
                question_limit=20,
                guess_limit=3,
            ),
            TwentyQuestionsGame(
                session_id=v2.id,
                catalog_key="v2",
                answer="Half-Life",
                aliases_json="[]",
                dossier_json="{}",
                question_limit=None,
                guess_limit=None,
                questions_per_user=5,
                guesses_per_user=2,
            ),
        )
    )
    await session.commit()

    restored_legacy = await session.get(TwentyQuestionsGame, legacy.id)
    restored_v2 = await session.get(TwentyQuestionsGame, v2.id)
    assert (restored_legacy.question_limit, restored_legacy.guess_limit) == (20, 3)
    assert (restored_v2.question_limit, restored_v2.guess_limit) == (None, None)
    assert (
        restored_v2.rules_version,
        restored_v2.questions_per_user,
        restored_v2.guesses_per_user,
    ) == (
        1,
        5,
        2,
    )
