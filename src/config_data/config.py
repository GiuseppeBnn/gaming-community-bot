from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
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

    # Alert channel: the minimum level that reaches the admins' DMs. Raising it
    # to CRITICAL is also the off switch — no second variable just for that.
    alert_min_level: str = "WARNING"

    daily_reward_coins: int = 100
    # /daily resets at local midnight (scheduler_timezone). This is the extra
    # minimum gap since the previous claim, ANDed with the midnight rule so a
    # 23:59 claim can't be followed by another one at 00:01. Bounded < 24h because
    # 24h+ could push the next claim past a whole day and cost a streak the user
    # did nothing to lose (the invariant used to live only in this comment).
    daily_min_hours: int = Field(default=6, ge=1, lt=24)

    fsm_storage: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # Groq LLM (AI entertainment module)
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.6-27b"
    # qwen3.6 is a hybrid reasoning model: without this it writes its chain of
    # thought INSIDE `content` (`<think>…</think>`) and it would reach the group.
    # Model-specific — `openai/gpt-oss-*` rejects "none" — so it travels with
    # `groq_model` and is overridable from .env. Empty string → field omitted,
    # which is the way out for a model that does not take it.
    groq_reasoning_effort: str = "none"
    # Provider-neutral paid lane for conversational and one-shot AI. OpenRouter
    # model lists are ordered fallbacks; every route also carries a provider-side
    # maximum price, so a temporary promotional price cannot silently become an
    # expensive production bill.
    openrouter_api_key: str = ""
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_app_name: str = "Alduino"
    openrouter_chat_models: str = (
        "deepseek/deepseek-v4-flash-0731,deepseek/deepseek-v4-flash"
    )
    openrouter_fun_models: str = (
        "qwen/qwen3.7-flash,deepseek/deepseek-v4-flash"
    )
    openrouter_timeout_seconds: int = Field(default=20, ge=1)
    openrouter_max_prompt_price: Decimal = Field(default=Decimal("0.25"), gt=0)
    openrouter_max_completion_price: Decimal = Field(default=Decimal("0.60"), gt=0)
    # Calendar-month application cap, in addition to the limit on the OpenRouter
    # key itself. Zero is an explicit emergency/development opt-out.
    ai_monthly_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0)
    ai_entertainment_provider: Literal["groq", "openrouter"] = "groq"
    # Judge model for the guess games. Deliberately separate from `groq_model`:
    # a verdict needs STRICT structured output (constrained decoding), which Groq
    # supports only on `openai/gpt-oss-*`, so it cannot come back as prose. The
    # entertainment commands need the opposite — a model that plays along with
    # black satire — and gpt-oss refuses them outright. Two jobs, two models.
    groq_judge_model: str = "openai/gpt-oss-120b"
    # ge=1: a 0 s timeout makes every judge call fail instantly, which the game
    # would report to players as "non verificata" forever.
    guess_judge_timeout_seconds: int = Field(default=12, ge=1)
    # Structured provider for persistent AI games. Gemini 3.5 Flash supports
    # JSON Schema and level-based thinking; a separate key keeps the existing
    # Groq entertainment commands independently deployable.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "medium"
    gemini_timeout_seconds: int = Field(default=20, ge=1)
    # Alduino chat is intentionally independent from structured AI games: it can
    # move provider/model without changing 20 Domande.
    alduino_provider: Literal["openrouter", "gemini", "groq"] = "gemini"
    alduino_gemini_model: str = "gemini-3.6-flash"
    alduino_thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    alduino_fallback_to_groq: bool = True
    alduino_timeout_seconds: int = Field(default=15, ge=1)
    alduino_history_turns: int = Field(default=10, ge=1, le=30)
    alduino_history_chars: int = Field(default=8000, ge=2000, le=30000)
    alduino_memory_rows_per_group: int = Field(default=1000, ge=100, le=10000)
    # Ambient group context: stored locally in a bounded rolling window, then a
    # smaller newest-first slice is sent to the model. This requires BotFather
    # privacy mode to be disabled; otherwise Telegram never delivers ordinary
    # group messages to the bot.
    alduino_capture_group_context: bool = True
    alduino_group_context_messages: int = Field(default=80, ge=10, le=500)
    alduino_group_context_chars: int = Field(default=24000, ge=2000, le=100000)
    alduino_group_memory_rows: int = Field(default=3000, ge=100, le=20000)
    ai_game_claim_timeout_seconds: int = Field(default=45, ge=5)
    # IGDB is the primary catalog for 20 Domande. Empty credentials keep the
    # built-in catalog as a fully functional fallback. The popularity gate uses
    # IGDB's total_rating_count (users + external critics), not rating score:
    # famous divisive games are playable; obscure well-rated games are not.
    igdb_client_id: str = ""
    igdb_client_secret: str = ""
    igdb_catalog_size: int = Field(default=300, ge=50, le=2000)
    igdb_min_rating_count: int = Field(default=100, ge=1)
    igdb_min_catalog_entries: int = Field(default=50, ge=10, le=2000)
    igdb_sync_interval_hours: int = Field(default=24, ge=1)
    igdb_timeout_seconds: int = Field(default=20, ge=1)
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
    # Level curve (GTA-style): cost to go from level n to n+1 is
    # round(xp_level_base * xp_level_growth ** (n - 1)) → each level costs +15% more.
    #
    # Both bounds are load-bearing, not cosmetic. `level_for_xp` walks the curve with
    # `while floor + cost <= xp`, so it terminates only because the cost keeps
    # growing. A base of 0, or any growth < 1, drives the cost to 0 and the loop
    # never ends — synchronously, inside async handlers, so the event loop freezes
    # and the bot stops answering at all. `XP_LEVEL_GROWTH=0.15` instead of `1.15`
    # is one character away.
    xp_level_base: int = Field(default=100, ge=1)      # XP to go from level 1 → 2
    xp_level_growth: float = Field(default=1.15, ge=1.0)  # geometric growth (+15%)

    # Quiz mode
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

    # Guess games (Guess The Game · Sound Quest) — one engine, two games.
    # How many UN-JUDGED answers we accept from one player on one round before
    # pausing them. An unreachable judge never costs a real attempt — that is our
    # outage, not the player's mistake — so the bound is on how many we ACCEPT,
    # not on how many we refund. Without it, an outage would open an unlimited
    # submission channel exactly when the local exact-match is all that stands
    # between a player and brute force.
    guess_max_unverified: int = Field(default=3, ge=0)
    # Values SUGGESTED by the creation flow; the admin picks the real ones per
    # round. ge=1 on the attempts: a round nobody may ever answer is not a game.
    guess_default_attempts: int = Field(default=5, ge=1)
    guess_default_time_limit_seconds: int = 300   # per player, 0 = no limit
    # How long a round stays open before it closes itself. The attempt clock is
    # per player and starts when they open the link, so there is no derivable
    # instant at which "everyone has expired" — the admin sets the round's own
    # length, and it is shown in the group announcement. 0 = close it by hand.
    guess_default_round_duration_seconds: int = 1800
    guess_answer_cooldown_seconds: int = 3        # between two submissions, per player
    # Per-rank prize defaults. The last-place floor is NOT duplicated here: it is
    # derived by `services.prizes.participation_floor` from the shared
    # `quiz_participation_floor_*` — one schedule for every game with a podium.
    guess_default_first: int = 1000
    guess_default_second: int = 500
    guess_default_third: int = 250
    guess_default_consolation: int = 100
    # Guess event XP (uncapped, like the quiz: admin-gated, not farmable).
    guess_xp_participation: int = 15   # XP for submitting at least one answer
    guess_xp_solved: int = 25          # extra XP for actually guessing it
    guess_xp_podium_first: int = 50
    guess_xp_podium_second: int = 30
    guess_xp_podium_third: int = 20

    # Shop cosmetics: how many purchased tags a user can keep active at once
    # (they can switch among owned tags and combine several). Raise to allow more.
    max_active_tags: int = 3

    # Scheduler (programmed quiz/poll/bet)
    scheduler_timezone: str = "Europe/Rome"
    # ge=1: the loop is `while True: ... await asyncio.sleep(interval)`, and asyncio
    # treats 0 (or negative) as "don't yield for long" → a hot loop that pegs a core
    # and starves every other task, polling included.
    scheduler_poll_interval: int = Field(default=20, ge=1)  # seconds between due-task checks

    # Backup & state export (see §25). All optional: with the Telethon creds
    # empty the chat archive stays disabled and the bot runs normally; the DB
    # state export needs no Telegram access and always works.
    backup_dir: str = "backups"                 # dir for snapshots + chat archive
    backup_state_interval_hours: int = 24       # how often the loop exports DB state
    # ge=1: rotation prunes everything beyond `keep`, so 0 would delete every
    # snapshot including the one just written — a backup setting that erases backups.
    backup_state_keep: int = Field(default=5, ge=1)  # rotated state snapshots to retain
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

    @model_validator(mode="after")
    def validate_igdb_catalog_bounds(self) -> Self:
        if self.igdb_min_catalog_entries > self.igdb_catalog_size:
            raise ValueError("igdb_min_catalog_entries cannot exceed igdb_catalog_size")
        return self


settings = Settings()
