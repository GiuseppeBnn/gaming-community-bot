"""Unit tests for the admin dashboard keyboards (callback grammar + structure)."""

from __future__ import annotations

from types import SimpleNamespace


from keyboards.admin_dashboard_kb import (
    back_home_kb,
    confirm_kb,
    home_kb,
    user_detail_kb,
    users_kb,
)


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _cbs(markup):
    return [b.callback_data for b in _flat(markup) if b.callback_data]


class TestHome:
    def test_has_all_sections_and_close_last(self):
        cbs = _cbs(home_kb())
        # Quiz + Scommesse are now unified under the "🎬 Eventi" hub (ev:home).
        for expected in ("adm:stats", "adm:lead", "ev:home", "adm:users:0",
                         "adm:econ", "adm:audit", "adm:help"):
            assert expected in cbs
        assert _flat(home_kb())[-1].callback_data == "adm:close"

    def test_back_home_kb(self):
        cbs = _cbs(back_home_kb())
        assert cbs == ["adm:home", "adm:close"]


class TestUserDetail:
    def test_full_actions_when_group_enabled(self):
        cbs = _cbs(user_detail_kb(555, group_enabled=True))
        assert "adm:act:credit:555" in cbs
        assert "adm:act:debit:555" in cbs
        assert "adm:act:setbal:555" in cbs
        assert "adm:ask:ban:555" in cbs
        assert "adm:ask:kick:555" in cbs
        assert "adm:do:sban:555" in cbs
        assert "adm:act:mute:555" in cbs
        assert "adm:do:unmute:555" in cbs
        assert "adm:act:warn:555" in cbs
        assert "adm:do:unwarn:555" in cbs
        assert "adm:users:0" in cbs  # back to list
        assert "adm:close" in cbs

    def test_no_moderation_when_group_disabled(self):
        cbs = _cbs(user_detail_kb(555, group_enabled=False))
        # currency actions remain
        assert "adm:act:credit:555" in cbs
        assert "adm:act:setbal:555" in cbs
        # moderation actions are hidden
        assert not any("ban" in c or "mute" in c or "warn" in c for c in cbs)


class TestUsersPicker:
    def _users(self, n):
        return [(SimpleNamespace(tg_id=i, username=f"u{i}", full_name=f"User {i}"), i * 10)
                for i in range(1, n + 1)]

    def test_one_button_per_user(self):
        cbs = _cbs(users_kb(self._users(3), page=0, has_next=False))
        assert "adm:user:1" in cbs and "adm:user:2" in cbs and "adm:user:3" in cbs

    def test_no_prev_on_first_page(self):
        cbs = _cbs(users_kb(self._users(2), page=0, has_next=True))
        assert not any(c == "adm:users:-1" for c in cbs)
        assert "adm:users:1" in cbs  # next present

    def test_prev_and_next_on_middle_page(self):
        cbs = _cbs(users_kb(self._users(2), page=2, has_next=True))
        assert "adm:users:1" in cbs  # prev
        assert "adm:users:3" in cbs  # next

    def test_no_next_on_last_page(self):
        cbs = _cbs(users_kb(self._users(2), page=1, has_next=False))
        assert "adm:users:0" in cbs   # prev
        assert "adm:users:2" not in cbs
        assert "adm:search" in cbs
        assert "adm:home" in cbs


class TestConfirm:
    def test_confirm_and_cancel(self):
        cbs = _cbs(confirm_kb("ban", 777))
        assert cbs == ["adm:do:ban:777", "adm:user:777"]
