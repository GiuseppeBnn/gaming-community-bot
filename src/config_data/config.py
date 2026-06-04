from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    admin_ids: list[int] = []

    daily_reward_coins: int = 100

    fsm_storage: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # Groq LLM (AI entertainment module)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    ai_cooldown_seconds: int = 60   # anti-spam: 1 AI command / N s per non-admin

    # Warn/strike system (admin moderation)
    warn_mute_threshold: int = 3       # active warnings → auto-mute
    warn_ban_threshold: int = 5        # active warnings → auto-ban
    warn_mute_duration_seconds: int = 3600

    # XP progression (merit metric, kept separate from coins).
    # XP is earned from admin-curated events (quiz) uncapped, plus a small *capped*
    # daily participation quota so users can't farm XP from random actions.
    catalog_dir: str = "data"               # dir with optional trophies/ranks/cosmetics CSVs
    xp_daily_participation_cap: int = 50     # max farmable XP per user per day
    xp_per_daily_claim: int = 10             # capped XP granted on /daily
    xp_per_bet_won: int = 15                 # capped XP granted on a winning bet

    # Quiz mode
    quiz_default_prize: int = 1000     # legacy: coin prize pool split among top scorers
    quiz_xp_per_correct: int = 10      # XP awarded per correct answer (event XP, uncapped)
    # Per-rank prize defaults (suggested in the creation flow)
    quiz_default_first: int = 1000
    quiz_default_second: int = 500
    quiz_default_third: int = 250
    quiz_default_consolation: int = 100   # 4th place; consolation decreases from here
    # Guaranteed floor for the last finisher = max(floor_min, round(consolation * floor_ratio))
    quiz_participation_floor_ratio: float = 0.2
    quiz_participation_floor_min: int = 1

    # Scheduler (programmed quiz/poll/bet)
    scheduler_timezone: str = "Europe/Rome"
    scheduler_poll_interval: int = 20  # seconds between due-task checks

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: object) -> list[int]:
        if isinstance(v, list):
            return [int(i) for i in v]
        return [int(x.strip()) for x in str(v).split(",") if x.strip()]


settings = Settings()
