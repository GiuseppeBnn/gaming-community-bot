"""Unit tests for the admin dashboard keyboards (callback grammar + structure)."""

from __future__ import annotations

from types import SimpleNamespace

from handlers.callbacks import AdminCb, EventCb
from keyboards.admin_dashboard_kb import (
    back_home_kb,
    confirm_kb,
    home_kb,
    mass_confirm_kb,
    mass_more_kb,
    mass_picker_kb,
    mass_remove_kb,
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


class TestMassPicker:
    def _users(self, n):
        return [(SimpleNamespace(tg_id=i, username=f"u{i}", full_name=f"User {i}"), i * 10)
                for i in range(1, n + 1)]

    def _texts(self, markup):
        return [b.text for b in _flat(markup)]

    def test_selected_members_are_marked(self):
        markup = mass_picker_kb(self._users(3), selected_ids=[2], page=0, has_next=False)
        cbs = _cbs(markup)
        assert all(AdminCb(action="mrpick", item_id=i).pack() in cbs for i in (1, 2, 3))
        assert any(t.startswith("✅") for t in self._texts(markup))
        # A selection offers the confirm shortcut.
        assert AdminCb(action="mrconfirm").pack() in cbs

    def test_no_confirm_button_without_a_selection(self):
        cbs = _cbs(mass_picker_kb(self._users(2), selected_ids=[], page=0, has_next=False))
        assert AdminCb(action="mrconfirm").pack() not in cbs

    def test_prev_and_next_on_a_middle_page(self):
        cbs = _cbs(mass_picker_kb(self._users(2), selected_ids=[], page=2, has_next=True))
        assert AdminCb(action="mrlist", item_id=1).pack() in cbs  # prev
        assert AdminCb(action="mrlist", item_id=3).pack() in cbs  # next

    def test_search_mode_drops_paging_and_offers_the_full_list(self):
        cbs = _cbs(mass_picker_kb(self._users(2), selected_ids=[], page=3, has_next=True,
                                  is_search=True))
        assert AdminCb(action="mrlist", item_id=2).pack() not in cbs  # no next
        assert AdminCb(action="mrlist", item_id=0).pack() in cbs      # back to full list
        assert AdminCb(action="mrsearch").pack() in cbs

    def test_more_kb_offers_yes_and_no(self):
        cbs = _cbs(mass_more_kb())
        assert AdminCb(action="mrmore", key="yes").pack() in cbs
        assert AdminCb(action="mrmore", key="no").pack() in cbs

    def test_confirm_kb_has_send_add_remove_cancel(self):
        cbs = _cbs(mass_confirm_kb())
        assert AdminCb(action="mrsend").pack() in cbs
        assert AdminCb(action="mrlist", item_id=0).pack() in cbs
        assert AdminCb(action="mrremlist").pack() in cbs
        assert AdminCb(action="home").pack() in cbs

    def test_remove_kb_has_one_button_per_member(self):
        users = [SimpleNamespace(tg_id=i, username=f"u{i}", full_name=f"User {i}")
                 for i in (5, 6)]
        cbs = _cbs(mass_remove_kb(users))
        assert AdminCb(action="mrunpick", item_id=5).pack() in cbs
        assert AdminCb(action="mrunpick", item_id=6).pack() in cbs
        assert AdminCb(action="mrconfirm").pack() in cbs

    def test_picker_falls_back_to_full_name_without_username(self):
        user = SimpleNamespace(tg_id=9, username=None, full_name="Senza Handle")
        texts = self._texts(mass_picker_kb([(user, 0)], selected_ids=[], page=0, has_next=False))
        assert any("Senza Handle" in t for t in texts)


class TestConfirm:
    def test_confirm_and_cancel(self):
        cbs = _cbs(confirm_kb("ban", 777))
        assert cbs == [
            AdminCb(action="do", key="ban", item_id=777).pack(),
            AdminCb(action="user", item_id=777).pack(),
        ]
