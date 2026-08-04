"""Unit tests for the admin dashboard keyboards (callback grammar + structure)."""

from __future__ import annotations

from types import SimpleNamespace

from handlers.callbacks import AdminCb, EventCb
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
        # Quiz + Scommesse are now unified under the "🎬 Eventi" hub (EventCb home).
        for expected in (
            AdminCb(action="stats").pack(),
            AdminCb(action="lead").pack(),
            EventCb(action="home").pack(),
            AdminCb(action="users", item_id=0).pack(),
            AdminCb(action="econ").pack(),
            AdminCb(action="audit").pack(),
            AdminCb(action="help").pack(),
        ):
            assert expected in cbs
        assert _flat(home_kb())[-1].callback_data == AdminCb(action="close").pack()

    def test_back_home_kb(self):
        cbs = _cbs(back_home_kb())
        assert cbs == [AdminCb(action="home").pack(), AdminCb(action="close").pack()]


class TestUserDetail:
    def test_full_actions_when_group_enabled(self):
        cbs = _cbs(user_detail_kb(555, group_enabled=True))
        assert AdminCb(action="act", key="credit", item_id=555).pack() in cbs
        assert AdminCb(action="act", key="debit", item_id=555).pack() in cbs
        assert AdminCb(action="act", key="setbal", item_id=555).pack() in cbs
        assert AdminCb(action="ask", key="ban", item_id=555).pack() in cbs
        assert AdminCb(action="ask", key="kick", item_id=555).pack() in cbs
        assert AdminCb(action="do", key="sban", item_id=555).pack() in cbs
        assert AdminCb(action="act", key="mute", item_id=555).pack() in cbs
        assert AdminCb(action="do", key="unmute", item_id=555).pack() in cbs
        assert AdminCb(action="act", key="warn", item_id=555).pack() in cbs
        assert AdminCb(action="do", key="unwarn", item_id=555).pack() in cbs
        assert AdminCb(action="users", item_id=0).pack() in cbs  # back to list
        assert AdminCb(action="close").pack() in cbs

    def test_no_moderation_when_group_disabled(self):
        cbs = _cbs(user_detail_kb(555, group_enabled=False))
        # currency actions remain
        assert AdminCb(action="act", key="credit", item_id=555).pack() in cbs
        assert AdminCb(action="act", key="setbal", item_id=555).pack() in cbs
        # moderation actions are hidden
        assert not any("ban" in c or "mute" in c or "warn" in c for c in cbs)


class TestUsersPicker:
    def _users(self, n):
        return [(SimpleNamespace(tg_id=i, username=f"u{i}", full_name=f"User {i}"), i * 10)
                for i in range(1, n + 1)]

    def test_one_button_per_user(self):
        cbs = _cbs(users_kb(self._users(3), page=0, has_next=False))
        assert all(AdminCb(action="user", item_id=i).pack() in cbs for i in (1, 2, 3))

    def test_no_prev_on_first_page(self):
        cbs = _cbs(users_kb(self._users(2), page=0, has_next=True))
        assert AdminCb(action="users", item_id=-1).pack() not in cbs
        assert AdminCb(action="users", item_id=1).pack() in cbs  # next present

    def test_prev_and_next_on_middle_page(self):
        cbs = _cbs(users_kb(self._users(2), page=2, has_next=True))
        assert AdminCb(action="users", item_id=1).pack() in cbs  # prev
        assert AdminCb(action="users", item_id=3).pack() in cbs  # next

    def test_no_next_on_last_page(self):
        cbs = _cbs(users_kb(self._users(2), page=1, has_next=False))
        assert AdminCb(action="users", item_id=0).pack() in cbs   # prev
        assert AdminCb(action="users", item_id=2).pack() not in cbs
        assert AdminCb(action="search").pack() in cbs
        assert AdminCb(action="home").pack() in cbs


class TestConfirm:
    def test_confirm_and_cancel(self):
        cbs = _cbs(confirm_kb("ban", 777))
        assert cbs == [
            AdminCb(action="do", key="ban", item_id=777).pack(),
            AdminCb(action="user", item_id=777).pack(),
        ]
