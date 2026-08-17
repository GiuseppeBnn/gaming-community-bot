from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config_data.config import settings
from database.models import Base

engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_MIGRATIONS: list[str] = [
    # badges: columns added after initial deploy
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS condition_type VARCHAR(64)",
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS condition_value INTEGER",
    # users: counters added after initial deploy
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bets_won INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transfers_made INTEGER NOT NULL DEFAULT 0",
    # users: bot-level ban flag (silent-drop middleware) added after initial deploy
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT false",
    # quizzes: per-rank custom prizes + decreasing consolation (added after initial deploy)
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_first INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_second INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_third INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_consolation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_min INTEGER NOT NULL DEFAULT 0",
    # users: progression / cosmetics + daily-XP cap bookkeeping (added after initial deploy)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS cosmetic_tag VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active_tags_json VARCHAR(512)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_slug VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp_today_date VARCHAR(10)",
    # badges → trophies: PlayStation-style rarity tier
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS rarity VARCHAR(16) NOT NULL DEFAULT 'bronze'",
    # badges → trophies: scope for parametrized conditions (item/category/game/collection)
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS condition_param VARCHAR(128)",
    # badges → trophies: hidden ("secret") flag, masked in the catalog until earned
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false",
    # wallets/ledger: balances can exceed int32 (airdrops/payouts accumulate).
    # ALTER TYPE to the same type is a no-op → idempotent like the entries above.
    "ALTER TABLE wallets ALTER COLUMN coins TYPE BIGINT",
    "ALTER TABLE ledger ALTER COLUMN amount TYPE BIGINT",
    # Same reasoning for the two counters that accumulate without a bound of their
    # own: XP grows with airdrops, a wager pot is denominated in (BIGINT) coins.
    "ALTER TABLE users ALTER COLUMN xp TYPE BIGINT",
    "ALTER TABLE betting_options ALTER COLUMN total_wagered TYPE BIGINT",
    # betting_events: timed betting window (NULL = illimitata) + armed close deadline.
    "ALTER TABLE betting_events ADD COLUMN IF NOT EXISTS betting_window_seconds INTEGER",
    "ALTER TABLE betting_events ADD COLUMN IF NOT EXISTS closes_at TIMESTAMP",
    # quizzes: per-user display randomization of question/answer order (added after initial deploy)
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS randomize_questions BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS randomize_answers BOOLEAN NOT NULL DEFAULT false",
    # ledger: /storico filtered `from_tg_id OR to_tg_id` + sorted by created_at
    # against a table with no index but the PK. See LedgerEntry.__table_args__ for
    # why two composite indexes and not three single-column ones. create_all()
    # skips existing tables, so an already-deployed DB only gets them from here.
    # ponytail: plain CREATE INDEX takes a write lock for the duration — fine at
    # this table's size; switch to CREATE INDEX CONCURRENTLY (which cannot run
    # inside the transaction below) if the ledger ever gets big enough to notice.
    "CREATE INDEX IF NOT EXISTS ix_ledger_from_created ON ledger (from_tg_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_to_created ON ledger (to_tg_id, created_at)",
    # guess_rounds: the round's own lifetime, which drives the auto-close task
    # (STEERING §19.b). Distinct from time_limit_seconds, which is per player.
    # DEFAULT 0 = "close it by hand", so rounds created before this column keep
    # behaving exactly as they did.
    "ALTER TABLE guess_rounds ADD COLUMN IF NOT EXISTS "
    "round_duration_seconds INTEGER NOT NULL DEFAULT 0",
    # guess_rounds: absolute auto-close instant, the admin-picked-date alternative
    # to round_duration_seconds (STEERING §19.b). NULL = fall back to the duration,
    # so rounds created before this column keep behaving exactly as they did.
    "ALTER TABLE guess_rounds ADD COLUMN IF NOT EXISTS closes_at TIMESTAMP",
    # Raid was removed from the product. Preserve its historical rows, but make
    # every still-live aggregate and timer inert so the scheduler cannot emit a
    # failure alert later for an event type that intentionally no longer exists.
    "UPDATE scheduled_tasks SET status = 'cancelled', "
    "executed_at = COALESCE(executed_at, CURRENT_TIMESTAMP), error = NULL "
    "WHERE task_type = 'raid' AND status = 'pending'",
    "UPDATE ai_game_sessions SET status = 'finished', "
    "finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP), "
    "pending_token = NULL, pending_since = NULL "
    "WHERE game_type = 'raid' AND status IN ('ready', 'running')",
]


async def run_migrations() -> None:
    """Apply idempotent schema migrations and feature-retirement maintenance.

    Only runs on PostgreSQL — SQLite test DBs are always created fresh by create_tables().
    """
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))
