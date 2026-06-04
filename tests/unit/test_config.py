"""Tests for config_data.config — Settings parsing and defaults."""

from __future__ import annotations

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
