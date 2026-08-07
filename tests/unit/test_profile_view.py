"""profile_text — the shared profile card text. One code path for /profilo and the
inline picker, so they cannot drift apart."""

from __future__ import annotations

from database.models import User, Wallet
from utils import profile_view


def _make_user(**kw) -> User:
    defaults = dict(
        tg_id=321, username="mario", full_name="Mario Rossi", xp=500,
        active_tags_json=[], rank_slug=None, wallet=Wallet(tg_id=321, coins=777),
    )
    defaults.update(kw)
    return User(**defaults)


class TestProfileText:
    def test_balance_and_rank_are_included(self):
        text = profile_view.profile_text(_make_user())

        assert "777" in text                       # saldo incluso (decisione utente)
        assert "Livello" in text
        assert "Trofei" in text

    def test_username_is_escaped(self):
        text = profile_view.profile_text(_make_user(username="a<b>"))

        assert "&lt;b&gt;" in text and "<b>a<b>" not in text

    def test_no_username_renders_N_D(self):
        text = profile_view.profile_text(_make_user(username=None))

        assert "N/D" in text

    def test_full_name_is_escaped_even_without_tags(self):
        text = profile_view.profile_text(_make_user(full_name="x<y> & z"))

        assert "x&lt;y&gt;" in text

    def test_pantry_section_appears_only_when_provided(self):
        from services.catalog_loader import ConsumableItem
        pantry = [(ConsumableItem("cons_revive", "Rivivere", "💖", "cons_power", 50, "revive"), 2)]

        with_pantry = profile_view.profile_text(_make_user(), pantry=pantry)
        without = profile_view.profile_text(_make_user())

        assert "Dispensa" in with_pantry and "Dispensa" not in without

    def test_trophies_count_comes_from_badges(self):
        # badges è una relationship: serve un utente con lista pre-caricata via selectinload.
        from database.models import UserBadge
        user = _make_user()
        user.badges = [
            UserBadge(user_tg_id=321, badge_id=1),
            UserBadge(user_tg_id=321, badge_id=2),
            UserBadge(user_tg_id=321, badge_id=3),
        ]

        text = profile_view.profile_text(user)

        assert "Trofei:</b> 3" in text
