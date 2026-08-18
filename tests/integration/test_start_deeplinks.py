"""`/start` and its deep-links — `handlers/common.py`.

Every screen the bot can open from outside a chat goes through `cmd_start`: the
group posts a `t.me/bot?start=<payload>` button, the user taps it, and this one
handler decides what happens. It was 36% covered.

Two properties are worth more than the dispatch table itself:

  * **the admin re-check**. This router is public and the payload is just text in a
    URL — anyone can type `?start=admin`. So each admin entry point re-checks
    `is_admin` *here*, and does not trust that the caller came through the group
    command's admin filter. The parametrized test below asserts, for every admin
    payload, that a non-admin gets refused **and that the target is never called**;
  * **the onboarding gate does not trap admins**. Rules acceptance is personal and
    happens in private, but a group admin who lands here from `?start=create_quiz`
    has never done the private onboarding — gating them would make the admin
    deep-links unreachable for exactly the people they exist for.

Targets are patched at their own module (the handler imports them lazily inside the
function, so the attribute is looked up at call time). This keeps the test about
routing, not about the twenty screens it can route to — those have their own files.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import filters.admin_filter as admin_filter
from handlers import common
from utils import cooldown

USER_ID = 7
GROUP_CHAT = -100_123


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")

    async def get_chat_administrators(self, chat_id):
        raise RuntimeError("no telegram in tests")  # → admin_ids only


class _FakeMessage:
    def __init__(self, *, chat_type: str = "private", user_id: int = USER_ID,
                 username: str | None = "tizio", text: str = "/start") -> None:
        self.text = text
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(
            id=user_id, username=username, first_name="Tizio", full_name="Tizio Test"
        )
        self.chat = SimpleNamespace(
            id=user_id if chat_type == "private" else GROUP_CHAT, type=chat_type
        )
        self.message_id = 1
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _Spy:
    """Stands in for the screen a deep-link opens; records that it was reached."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def called(self) -> bool:
        return bool(self.calls)


def _command(payload: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(command="start", args=payload)


def _state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID))


@pytest.fixture(autouse=True)
def _no_admins(monkeypatch):
    """Nobody is an admin unless a test says so, and no cooldown leaks in."""
    monkeypatch.setattr(admin_filter.settings, "admin_ids", [])
    admin_filter._cache.clear()
    cooldown.reset()
    yield
    admin_filter._cache.clear()
    cooldown.reset()


@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(admin_filter.settings, "admin_ids", [USER_ID])
    admin_filter._cache.clear()


@pytest.fixture
async def onboarded(session, user_factory):
    await user_factory(tg_id=USER_ID, username="tizio", coins=100,
                       onboarding_completed=True)


def _patch(monkeypatch, dotted: str) -> _Spy:
    """Replace `module.attr` with a spy, given "package.module.attr"."""
    module_name, attr = dotted.rsplit(".", 1)
    module = importlib.import_module(module_name)
    spy = _Spy()
    monkeypatch.setattr(module, attr, spy)
    return spy


# Every payload that must be gated, and the screen it would otherwise open.
ADMIN_PAYLOADS = [
    ("admin", "handlers.admin_dashboard.show_dashboard_home"),
    ("eventi", "handlers.events.show_hub"),
    ("create_quiz", "handlers.quiz.start_quiz_creation"),
    ("create_poll", "handlers.events.start_poll_creation"),
    ("programma", "handlers.schedule.start_schedule_flow"),
    ("manage_bets", "handlers.admin_betting._show_event_list"),
    ("backup", "handlers.backup.run_backup_now"),
    ("esporta", "handlers.backup.run_export_now"),
]

# Payloads any member may use, and where they land.
PUBLIC_PAYLOADS = [
    ("quiz_5", "handlers.quiz.start_quiz_session"),
    # The guess games are public: any group member plays them, so — unlike the
    # admin landings — there is no is_admin re-check here to forget.
    ("guess_5", "handlers.guess.start_guess_session"),
    ("sound_5", "handlers.guess.start_guess_session"),
    ("shop_-100123", "handlers.shop.start_shop_private"),
    ("saldo", "handlers.economy.show_saldo"),
    ("daily", "handlers.economy.show_saldo"),
    ("storico", "handlers.economy.show_storico"),
    ("trofei", "handlers.badges.show_traguardi"),
    ("traguardi", "handlers.badges.show_traguardi"),
    ("classifiche", "handlers.leaderboard.show_board_private"),
    ("scommesse", "handlers.betting.show_events_private"),
    ("create_bet", "handlers.betting.start_bet_creation"),
    ("bet_7", "handlers.betting.start_bet_view"),
    ("bet_custom_1_2", "handlers.betting.start_custom_amount"),
]


# ---------------------------------------------------------------------------
# The admin gate on the deep-links
# ---------------------------------------------------------------------------

class TestAdminDeepLinks:
    @pytest.mark.parametrize("payload,target", ADMIN_PAYLOADS)
    async def test_a_non_admin_is_refused_and_the_screen_never_opens(
        self, session, onboarded, monkeypatch, payload, target
    ):
        """The refusal message is the visible half; the invisible half — that the
        admin screen is not built at all — is the one that matters."""
        spy = _patch(monkeypatch, target)
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert "non autorizzato" in message.said
        assert not spy.called

    @pytest.mark.parametrize("payload,target", ADMIN_PAYLOADS)
    async def test_an_admin_reaches_the_screen(
        self, session, onboarded, monkeypatch, as_admin, payload, target
    ):
        spy = _patch(monkeypatch, target)
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert spy.called
        assert "non autorizzato" not in message.said


# ---------------------------------------------------------------------------
# The public deep-links
# ---------------------------------------------------------------------------

class TestPublicDeepLinks:
    @pytest.mark.parametrize("payload,target", PUBLIC_PAYLOADS)
    async def test_each_payload_opens_its_own_screen(
        self, session, onboarded, monkeypatch, payload, target
    ):
        spy = _patch(monkeypatch, target)
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert spy.called, f"{payload} did not reach {target}"

    async def test_the_custom_bet_link_carries_event_and_option(
        self, session, onboarded, monkeypatch
    ):
        """These two integers decide which bet the typed amount is placed on, so a
        link that parses "loosely" would put money on the wrong event."""
        spy = _patch(monkeypatch, "handlers.betting.start_custom_amount")
        message = _FakeMessage()

        await common.cmd_start(message, _command("bet_custom_12_34"), _state(), session)

        assert spy.calls[0][0][2:] == (12, 34)

    @pytest.mark.parametrize("payload", ["bet_custom_x_2", "bet_custom_1", "bet_custom_1_2_3"])
    async def test_a_malformed_custom_bet_link_opens_nothing(
        self, session, onboarded, monkeypatch, payload
    ):
        spy = _patch(monkeypatch, "handlers.betting.start_custom_amount")
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert not spy.called
        assert "Link non valido" in message.said

    async def test_a_malformed_bet_link_opens_nothing(self, session, onboarded, monkeypatch):
        spy = _patch(monkeypatch, "handlers.betting.start_bet_view")
        message = _FakeMessage()

        await common.cmd_start(message, _command("bet_abc"), _state(), session)

        assert not spy.called
        assert "Link non valido" in message.said

    async def test_the_profile_payload_is_served_in_place(self, session, onboarded):
        message = _FakeMessage()

        await common.cmd_start(message, _command("profilo"), _state(), session)

        assert "Username" in message.said

    async def test_the_help_payload_shows_the_legend(self, session, onboarded):
        message = _FakeMessage()

        await common.cmd_start(message, _command("help"), _state(), session)

        assert message.said

    async def test_the_help_legend_hides_admin_commands_from_members(
        self, session, onboarded
    ):
        """The legend is rendered with the caller's own admin flag: a member must
        not learn the admin command set from a public deep-link."""
        member = _FakeMessage()
        await common.cmd_start(member, _command("help"), _state(), session)

        assert "/admin" not in member.said

    async def test_the_help_legend_shows_them_to_an_admin(
        self, session, onboarded, as_admin
    ):
        admin = _FakeMessage()
        await common.cmd_start(admin, _command("help"), _state(), session)

        assert "/admin" in admin.said

    async def test_the_spiega_payload_explains_that_command(self, session, onboarded):
        message = _FakeMessage()

        await common.cmd_start(message, _command("spiega_daily"), _state(), session)

        assert "daily" in message.said.lower()

    async def test_no_payload_shows_the_menu(self, session, onboarded):
        message = _FakeMessage()

        await common.cmd_start(message, _command(), _state(), session)

        assert "Bentornato" in message.said and "/profilo" in message.said

    async def test_the_menu_greets_a_user_without_a_username(self, session, user_factory):
        await user_factory(tg_id=USER_ID, username=None, onboarding_completed=True)
        message = _FakeMessage(username=None)

        await common.cmd_start(message, _command(), _state(), session)

        assert "Bentornato" in message.said and "@" not in message.said


# ---------------------------------------------------------------------------
# The onboarding gate
# ---------------------------------------------------------------------------

class TestOnboardingGate:
    async def test_an_unknown_user_in_private_gets_the_rules(self, session):
        message = _FakeMessage()

        await common.cmd_start(message, _command(), _state(), session)

        assert "regole" in message.said.lower()

    async def test_an_unknown_user_in_the_group_is_sent_to_private(self, session):
        """The accept button is one shared message in the group: whoever taps it
        completes THEIR onboarding, not the caller's."""
        message = _FakeMessage(chat_type="supergroup")

        await common.cmd_start(message, _command(), _state(), session)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=onboarding")

    async def test_a_user_who_never_finished_onboarding_is_gated_again(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio", onboarding_completed=False)
        message = _FakeMessage()

        await common.cmd_start(message, _command("saldo"), _state(), session)

        assert "regole" in message.said.lower()

    async def test_an_admin_is_not_trapped_by_the_gate(
        self, session, monkeypatch, as_admin
    ):
        """A group admin is recognised through Telegram, not through the private
        onboarding: gating them here makes every admin deep-link unreachable."""
        spy = _patch(monkeypatch, "handlers.events.show_hub")
        message = _FakeMessage()

        await common.cmd_start(message, _command("eventi"), _state(), session)

        assert spy.called


# ---------------------------------------------------------------------------
# manage_quiz / manage_guess / manage_sound — the admin management lists
# ---------------------------------------------------------------------------

_MANAGE = [
    ("manage_quiz", "handlers.event_types.quiz_type", "QuizType"),
    ("manage_guess", "handlers.event_types.guess_type", "GuessType"),
    ("manage_sound", "handlers.event_types.guess_type", "GuessType"),
]


def _spy_render_list(monkeypatch, module_name: str, cls_name: str, calls: list) -> None:
    cls = getattr(importlib.import_module(module_name), cls_name)

    async def fake(self, message, db_session):
        calls.append(cls_name)

    monkeypatch.setattr(cls, "render_list", fake)


class TestManageDeepLinks:
    """/quiz, /guessTheGame and /soundQuest send an admin from the group straight to
    that type's own list — not the /admin dashboard (the bug this fixes)."""

    @pytest.mark.parametrize("payload,module_name,cls_name", _MANAGE)
    async def test_an_admin_reaches_the_type_list(
        self, session, onboarded, monkeypatch, as_admin, payload, module_name, cls_name
    ):
        calls: list[str] = []
        _spy_render_list(monkeypatch, module_name, cls_name, calls)
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert calls == [cls_name] and "non autorizzato" not in message.said

    @pytest.mark.parametrize("payload", ["manage_quiz", "manage_guess", "manage_sound"])
    async def test_a_non_admin_is_refused(self, session, onboarded, monkeypatch, payload):
        calls: list[str] = []
        _spy_render_list(monkeypatch, "handlers.event_types.quiz_type", "QuizType", calls)
        _spy_render_list(monkeypatch, "handlers.event_types.guess_type", "GuessType", calls)
        message = _FakeMessage()

        await common.cmd_start(message, _command(payload), _state(), session)

        assert "non autorizzato" in message.said and not calls


# ---------------------------------------------------------------------------
# Welcome-trophy backfill (fix: an admin who bypassed the rules card can miss it)
# ---------------------------------------------------------------------------

class TestWelcomeTrophyBackfill:
    async def _first_steps_count(self, session, tg_id: int) -> int:
        from sqlalchemy import func, select

        from database.models import Badge, UserBadge

        return await session.scalar(
            select(func.count())
            .select_from(UserBadge)
            .join(Badge, Badge.id == UserBadge.badge_id)
            .where(UserBadge.user_tg_id == tg_id, Badge.slug == "first_steps")
        )

    async def test_an_admin_who_never_onboarded_gets_it_by_using_the_bot(
        self, seeded_session, user_factory, monkeypatch, as_admin
    ):
        # A Telegram-recognized admin who bypassed the rules card: no first_steps yet.
        await user_factory(tg_id=USER_ID, username="tizio", onboarding_completed=False)
        message = _FakeMessage()

        await common.cmd_start(message, _command(), _state(), seeded_session)

        assert await self._first_steps_count(seeded_session, USER_ID) == 1

    async def test_it_is_idempotent_for_someone_who_already_has_it(
        self, seeded_session, user_factory, monkeypatch, as_admin
    ):
        from services import badge_service

        await user_factory(tg_id=USER_ID, username="tizio", onboarding_completed=True)
        await badge_service.award_badge(seeded_session, USER_ID, badge_service.BADGE_FIRST_STEPS)
        await seeded_session.commit()

        await common.cmd_start(_FakeMessage(), _command(), _state(), seeded_session)

        assert await self._first_steps_count(seeded_session, USER_ID) == 1  # not doubled

    async def test_a_gated_non_admin_does_not_get_it_before_accepting_the_rules(
        self, seeded_session, user_factory
    ):
        # Not onboarded, not admin → the rules gate returns before the backfill runs.
        await user_factory(tg_id=USER_ID, username="tizio", onboarding_completed=False)
        message = _FakeMessage()

        await common.cmd_start(message, _command(), _state(), seeded_session)

        assert "regole" in message.said.lower()
        assert await self._first_steps_count(seeded_session, USER_ID) == 0


# ---------------------------------------------------------------------------
# /profilo
# ---------------------------------------------------------------------------

class TestProfilo:
    async def test_an_unregistered_user_is_told_to_start(self, session):
        message = _FakeMessage()

        await common.show_profilo(message, session)

        assert "/start" in message.said

    async def test_the_profile_shows_balance_level_and_trophies(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio", coins=1234, xp=50)
        message = _FakeMessage()

        await common.show_profilo(message, session)

        assert "1,234" in message.said
        assert "Livello" in message.said and "Trofei" in message.said

    async def test_the_profile_never_shows_the_telegram_id(self, session, user_factory):
        """The id is the admin dossier's business (/info), not a public profile's —
        and /profilo answers in the group."""
        await user_factory(tg_id=USER_ID, username="tizio", coins=10)
        message = _FakeMessage(chat_type="supergroup")

        await common.show_profilo(message, session)

        assert str(USER_ID) not in message.said

    async def test_a_user_without_a_username_shows_a_placeholder(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username=None, coins=10)
        message = _FakeMessage(username=None)

        await common.show_profilo(message, session)

        assert "N/D" in message.said

    async def test_the_pantry_lists_what_the_user_owns(self, session, user_factory):
        from services import consumable_service

        await user_factory(tg_id=USER_ID, username="tizio", coins=1000)
        gelato = consumable_service.get_item("cons_gelato_sale_marino")
        await consumable_service.record_consumption(session, USER_ID, gelato, gelato.price)
        await session.commit()
        message = _FakeMessage()

        await common.show_profilo(message, session)

        assert "Dispensa" in message.said and gelato.emoji in message.said

    async def test_an_active_tag_is_shown_next_to_the_name(self, session, user_factory):
        """The flair a user paid for has to appear where they can see it, otherwise
        the shop sells something invisible."""
        await user_factory(tg_id=USER_ID, username="tizio", coins=10,
                           cosmetic_tag="👑 Reietto")
        message = _FakeMessage()

        await common.show_profilo(message, session)

        assert "👑 Reietto" in message.said
        assert "Tag:" in message.said

    async def test_an_empty_pantry_is_not_mentioned_at_all(self, session, user_factory):
        await user_factory(tg_id=USER_ID, username="tizio", coins=1000)
        message = _FakeMessage()

        await common.show_profilo(message, session)

        assert "Dispensa" not in message.said

    async def test_the_command_is_rate_limited(self, session, user_factory):
        """/profilo answers in the group, so without a throttle one user could push
        the chat with it. The second call within the window says nothing at all."""
        await user_factory(tg_id=USER_ID, username="tizio", coins=10)
        first, second = _FakeMessage(chat_type="supergroup"), _FakeMessage(chat_type="supergroup")

        await common.cmd_profilo(first, session)
        await common.cmd_profilo(second, session)

        assert first.said and second.said == ""


# ---------------------------------------------------------------------------
# /comandi and /spiega_comando
# ---------------------------------------------------------------------------

class TestHelp:
    async def test_in_private_it_prints_the_legend(self, session):
        message = _FakeMessage()

        await common.cmd_help(message)

        assert message.said

    async def test_in_the_group_it_only_offers_a_button(self, session):
        """The full legend in the group is a wall of text everyone re-triggers."""
        message = _FakeMessage(chat_type="supergroup")

        await common.cmd_help(message)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=help")
        assert "/profilo" not in message.said

    async def test_the_group_button_is_rate_limited(self, session):
        first, second = _FakeMessage(chat_type="supergroup"), _FakeMessage(chat_type="supergroup")

        await common.cmd_help(first)
        await common.cmd_help(second)

        assert first.said and second.said == ""


class TestSpiegaComando:
    async def test_in_the_group_it_carries_the_command_through_the_link(self, session):
        message = _FakeMessage(chat_type="supergroup")

        await common.cmd_spiega_comando(message, SimpleNamespace(args="daily"))

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=spiega_daily")

    async def test_a_command_with_odd_characters_falls_back_to_the_general_guide(
        self, session
    ):
        """The payload charset is narrower than what a user can type; an unusable
        payload must degrade to the guide, not produce a broken link."""
        message = _FakeMessage(chat_type="supergroup")

        await common.cmd_spiega_comando(message, SimpleNamespace(args="dai ly!!"))

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=help")

    async def test_in_private_without_an_argument_it_shows_how_to_use_it(self, session):
        message = _FakeMessage()

        await common.cmd_spiega_comando(message, SimpleNamespace(args=None))

        assert "Uso:" in message.said

    async def test_in_private_with_an_argument_it_explains_the_command(self, session):
        message = _FakeMessage()

        await common.cmd_spiega_comando(message, SimpleNamespace(args="daily"))

        assert "daily" in message.said.lower()

    async def test_it_is_rate_limited_in_private(self, session):
        first, second = _FakeMessage(), _FakeMessage()

        await common.cmd_spiega_comando(first, SimpleNamespace(args="daily"))
        await common.cmd_spiega_comando(second, SimpleNamespace(args="daily"))

        assert "più piano" in second.said.lower()
