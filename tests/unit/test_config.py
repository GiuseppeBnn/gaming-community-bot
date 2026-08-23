"""Tests for config_data.config — Settings parsing and defaults."""

from __future__ import annotations

from decimal import Decimal

import pytest


class TestParseAdminIds:
    """admin_ids field validator accepts multiple input formats."""

    def test_comma_separated_string(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", admin_ids="111,222,333")  # type: ignore[call-arg]
        assert s.admin_ids == [111, 222, 333]

    def test_comma_separated_with_spaces(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", admin_ids=" 111 , 222 ")  # type: ignore[call-arg]
        assert s.admin_ids == [111, 222]

    def test_list_input_passthrough(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", admin_ids=[10, 20])  # type: ignore[call-arg]
        assert s.admin_ids == [10, 20]

    def test_empty_string_yields_empty_list(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", admin_ids="")  # type: ignore[call-arg]
        assert s.admin_ids == []

    def test_single_id(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", admin_ids="42")  # type: ignore[call-arg]
        assert s.admin_ids == [42]


class TestDefaults:
    """Verify Settings fields accept the expected documented values.

    Note: tests create Settings instances with explicit values because a .env
    file in the project root may override pydantic-settings defaults.
    """

    def test_db_url_accepts_sqlite(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", db_url="sqlite+aiosqlite:///./data/bot.db")  # type: ignore[call-arg]
        assert s.db_url.startswith("sqlite+aiosqlite")

    def test_group_id_zero_disables_guard(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", group_id=0)  # type: ignore[call-arg]
        assert s.group_id == 0

    def test_group_id_accepts_negative_value(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", group_id=-1001234567890)  # type: ignore[call-arg]
        assert s.group_id == -1001234567890

    def test_daily_reward_coins_field(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", daily_reward_coins=50)  # type: ignore[call-arg]
        assert s.daily_reward_coins == 50

    def test_fsm_storage_memory(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", fsm_storage="memory")  # type: ignore[call-arg]
        assert s.fsm_storage == "memory"

    def test_fsm_storage_redis(self):
        from config_data.config import Settings

        s = Settings(bot_token="x", fsm_storage="redis")  # type: ignore[call-arg]
        assert s.fsm_storage == "redis"


class TestBoundsThatPreventRealDamage:
    """Constraints only where an out-of-range value does something worse than being
    a silly setting. Deliberately **not** on all 50 settings for symmetry — the
    ones here each have a failure mode worth a test.
    """

    def test_a_level_growth_below_one_is_refused(self):
        """`XP_LEVEL_GROWTH=0.15` instead of `1.15` is a one-character typo that
        **freezes the whole bot**: see the decay test below for the mechanism.
        """
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(bot_token="x", xp_level_growth=0.15)  # type: ignore[call-arg]

    def test_a_zero_level_base_is_refused(self):
        """Same hazard from the other side: base 0 makes every level cost 0."""
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(bot_token="x", xp_level_base=0)  # type: ignore[call-arg]

    def test_a_growth_below_one_decays_the_level_cost_to_zero(self, monkeypatch):
        """Why the two constraints above exist, proved without hanging the suite.

        `level_for_xp` walks levels with `while floor + cost <= xp`, so it only
        terminates because `cost` keeps growing. Let the cost reach 0 and the loop
        never ends — and it is a *synchronous* call inside async handlers, so it
        takes the event loop with it: the bot stops answering entirely, on the first
        XP grant or profile view.

        Monkeypatching bypasses the validator on purpose: this asserts the hazard
        the validator now prevents.
        """
        from config_data.config import settings
        from services import xp_service

        monkeypatch.setattr(settings, "xp_level_base", 100)
        monkeypatch.setattr(settings, "xp_level_growth", 0.5)

        costs = [xp_service._level_cost(n) for n in range(1, 15)]
        assert 0 in costs, f"expected the cost to decay to 0, got {costs}"

    def test_daily_min_hours_must_stay_under_a_day(self):
        """A gap of 24h+ can push the next claim past a whole calendar day, so the
        user loses the streak through no fault of their own. The invariant was
        written in a comment; now it is enforced.
        """
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(bot_token="x", daily_min_hours=24)  # type: ignore[call-arg]

    def test_daily_min_hours_accepts_the_documented_range(self):
        from config_data.config import Settings

        assert Settings(bot_token="x", daily_min_hours=23).daily_min_hours == 23  # type: ignore[call-arg]

    def test_a_zero_scheduler_interval_is_refused(self):
        """`asyncio.sleep(0)` in the scheduler loop is a hot loop: it pegs a core
        and starves every other task on the event loop."""
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(bot_token="x", scheduler_poll_interval=0)  # type: ignore[call-arg]

    def test_a_zero_backup_keep_is_refused(self):
        """`backup_state_keep=0` would let rotation prune every snapshot, including
        the one just written — a backup setting that deletes backups."""
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(bot_token="x", backup_state_keep=0)  # type: ignore[call-arg]

    def test_igdb_quality_floor_cannot_exceed_requested_catalog(self):
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError):
            Settings(  # type: ignore[call-arg]
                bot_token="x", igdb_catalog_size=50, igdb_min_catalog_entries=51,
            )


class TestOpenRouterBudgetLanes:
    def test_documented_defaults_partition_the_global_cap(self):
        from config_data.config import Settings

        configured = Settings(bot_token="x", _env_file=None)  # type: ignore[call-arg]

        assert configured.ai_monthly_budget_usd == Decimal("5.00")
        assert configured.twentyq_openrouter_budget_usd == Decimal("4.00")
        assert configured.openrouter_other_budget_usd == Decimal("1.00")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("twentyq_openrouter_budget_usd", Decimal("-0.01")),
            ("openrouter_other_budget_usd", Decimal("-0.01")),
        ],
    )
    def test_lane_caps_cannot_be_negative(self, field, value):
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError) as error:
            Settings(bot_token="x", **{field: value})  # type: ignore[call-arg]
        assert error.value.errors()[0]["type"] == "greater_than_equal"

    def test_lane_caps_cannot_exceed_the_global_cap_in_sum(self):
        from pydantic import ValidationError

        from config_data.config import Settings

        with pytest.raises(ValidationError, match="lane budgets cannot exceed"):
            Settings(  # type: ignore[call-arg]
                bot_token="x",
                ai_monthly_budget_usd=Decimal("5.00"),
                twentyq_openrouter_budget_usd=Decimal("4.01"),
                openrouter_other_budget_usd=Decimal("1.00"),
            )

    def test_all_zero_caps_are_a_valid_shutdown_configuration(self):
        from config_data.config import Settings

        configured = Settings(  # type: ignore[call-arg]
            bot_token="x",
            ai_monthly_budget_usd=Decimal("0"),
            twentyq_openrouter_budget_usd=Decimal("0"),
            openrouter_other_budget_usd=Decimal("0"),
        )

        assert configured.ai_monthly_budget_usd == 0
        assert configured.twentyq_openrouter_budget_usd == 0
        assert configured.openrouter_other_budget_usd == 0
