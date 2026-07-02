from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    bot_token: str
    db_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # Telegram group ID (e.g. -1001234567890).
    # 0 = not configured → GroupMemberMiddleware skips the check.
    group_id: int = 0

    # NoDecode: keep pydantic-settings from JSON-decoding the env value, so the
    # CSV format (ADMIN_IDS=123,456) reaches parse_admin_ids as a plain string.
    admin_ids: Annotated[list[int], NoDecode] = []

    daily_reward_coins: int = 100

    fsm_storage: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # Groq LLM (AI entertainment module)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    ai_cooldown_seconds: int = 60   # anti-spam: 1 AI command / N s per non-admin
    # Per-command anti-spam cooldown (on top of the global rate-limit middleware).
    command_cooldown_seconds: int = 3        # heavier user commands, per non-admin
    event_create_cooldown_seconds: int = 5   # starting a quiz/poll/bet creation flow

    # Warn/strike system (admin moderation)
    warn_mute_threshold: int = 3       # active warnings → auto-mute
    warn_ban_threshold: int = 5        # active warnings → auto-ban
    warn_mute_duration_seconds: int = 3600

    # XP progression (merit metric, kept separate from coins). Two tiers:
    #   * EVENT XP (uncapped): admin-gated events you can't spam — quizzes and bets.
    #     These reward *participation first* and performance on top (see below), so
    #     showing up earns XP even without winning.
    #   * DAILY-QUOTA XP (capped): low-effort/recurring actions (/daily), bounded by
    #     `xp_daily_participation_cap` per user per day so they can't be farmed.
    catalog_dir: str = "data"               # dir with optional trophies/ranks/cosmetics CSVs
    xp_daily_participation_cap: int = 50     # max farmable (capped) XP per user per day
    xp_per_daily_claim: int = 10             # capped XP granted on /daily
    # Betting event XP (uncapped): placing a bet always pays participation XP; a
    # winning bet pays the (larger) win bonus on top at resolution.
    xp_per_bet_placed: int = 10              # participation XP for placing a bet
    xp_per_bet_won: int = 25                 # extra XP when that bet wins
    # Betting window: default minutes the pre-selected preset suggests at creation, and
    # the defensive fallback if the window step is somehow bypassed (0 = illimitata).
    bet_default_window_minutes: int = 60
    # Level curve (GTA-style): cost to go from level n to n+1 is
    # round(xp_level_base * xp_level_growth ** (n - 1)) → each level costs +15% more.
    xp_level_base: int = 100                 # XP to go from level 1 → 2
    xp_level_growth: float = 1.15            # geometric growth per level (+15%)

    # Quiz mode
    quiz_default_prize: int = 1000     # legacy: coin prize pool split among top scorers
    # Quiz event XP (uncapped). Every player who answers at least one question gets
    # `quiz_xp_participation`; each correct answer adds `quiz_xp_per_correct`; the
    # top-3 finishers get an extra podium bonus. Rewards participation, not just winning.
    quiz_xp_participation: int = 20    # XP just for playing (≥ 1 answer)
    quiz_xp_per_correct: int = 10      # XP per correct answer
    quiz_xp_podium_first: int = 50     # extra XP for the 1st-place finisher
    quiz_xp_podium_second: int = 30    # extra XP for the 2nd-place finisher
    quiz_xp_podium_third: int = 20     # extra XP for the 3rd-place finisher
    # Suggested per-question time limit in the creation flow (seconds; 0 = no limit).
    # The admin picks the actual value when building the quiz.
    quiz_default_time_limit_seconds: int = 30
    # Per-rank prize defaults (suggested in the creation flow)
    quiz_default_first: int = 1000
    quiz_default_second: int = 500
    quiz_default_third: int = 250
    quiz_default_consolation: int = 100   # 4th place; consolation decreases from here
    # Guaranteed floor for the last finisher = max(floor_min, round(consolation * floor_ratio))
    quiz_participation_floor_ratio: float = 0.2
    quiz_participation_floor_min: int = 1

    # Shop cosmetics: how many purchased tags a user can keep active at once
    # (they can switch among owned tags and combine several). Raise to allow more.
    max_active_tags: int = 3

    # Scheduler (programmed quiz/poll/bet)
    scheduler_timezone: str = "Europe/Rome"
    scheduler_poll_interval: int = 20  # seconds between due-task checks

    # Backup & state export (see §25). All optional: with the Telethon creds
    # empty the chat archive stays disabled and the bot runs normally; the DB
    # state export needs no Telegram access and always works.
    backup_dir: str = "backups"                 # dir for snapshots + chat archive
    backup_state_interval_hours: int = 24       # how often the loop exports DB state
    backup_state_keep: int = 5                  # rotated state snapshots to retain
    backup_chat_interval_hours: int = 168       # how often the loop extends the archive
    backup_max_message_chars: int = 4096        # per-message text cap in the archive
    # MTProto (Telethon) — reads the group history the Bot API cannot. The
    # session string is a SENSITIVE full-account credential: keep it in the .env
    # only, generate it once with scripts/login_telethon.py.
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = ""

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: object) -> list[int]:
        if isinstance(v, list):
            return [int(i) for i in v]
        return [int(x.strip()) for x in str(v).split(",") if x.strip()]


settings = Settings()
