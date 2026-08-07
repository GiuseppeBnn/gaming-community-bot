"""The player-facing betting flow — `handlers/betting.py`.

This file was 36% covered, and it is the surface where a *player* spends coins: the
event list, the option, the amount, the confirm button. `bet_service.place_bet` is
tested elsewhere; what was untested is everything around it — the layer that decides
which event and which amount get handed to the service, and what happens when the
answer is "none of them".

What is asserted, in order of how much it can cost:

  * **money**: the wallet after a confirm, and the wallet after every refusal
    (already bet / closed / not enough coins) — a refusal that still debits is the
    worst bug this file could have;
  * **the guards**: malformed callback data is user-supplied (an old keyboard, a
    replayed button) and must never reach `int()` unprotected;
  * **one live prompt per user**: `bet_active_msg_id` is deleted before a new one is
    sent, and a delete that fails must not stop the new message;
  * **best-effort really is best-effort**: a trophy announcement that explodes must
    not undo a bet that is already committed.

Balances are read with a *column* select (`select(Wallet.coins)`), never by touching
the entity: `expire_on_commit=False` means an entity select can be served from the
identity map and would assert the handler's own stale copy (STEERING §22).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import services.bet_service as bet_svc
from config_data.config import settings
from database.models import BettingEvent, EventStatus, ScheduledTask, UserBet, Wallet
from handlers import betting
from handlers.callbacks import (
    BetAmountCb,
    BetCb,
    BetConfirmCb,
    BetCustomCb,
    BetEventCb,
    BetOptionCb,
)
from services import group_registry
from utils import cooldown

PLAYER = 10
CREATOR = 1


# ---------------------------------------------------------------------------
# Stubs — the handlers are driven directly, no dispatcher and no network
# ---------------------------------------------------------------------------


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []

    async def get_me(self):
        return SimpleNamespace(username="testbot")

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _DeafBot(_FakeBot):
    """Deleting fails (message too old, already gone) and sending fails (blocked)."""

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        raise RuntimeError("message to delete not found")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        raise RuntimeError("Forbidden: bot was blocked")


class _FakeMessage:
    def __init__(
        self, text: str = "", *, user_id: int = PLAYER, chat_type: str = "private", bot=None
    ) -> None:
        self.text = text
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(
            id=user_id, username=f"u{user_id}", full_name=f"User {user_id}"
        )
        self.chat = SimpleNamespace(
            id=user_id if chat_type == "private" else -100_123, type=chat_type
        )
        self.message_id = 500
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=1000 + len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def delete(self):
        self.deleted = True

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _UndeletableMessage(_FakeMessage):
    async def delete(self):
        raise RuntimeError("message can't be deleted")


class _FakeCallback:
    def __init__(self, callback_data, message=None, user_id: int = PLAYER) -> None:
        self.data = callback_data.pack()
        self.callback_data = callback_data
        self.message = message or _FakeMessage(user_id=user_id)
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(
            id=user_id, username=f"u{user_id}", full_name=f"User {user_id}"
        )
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]

    @property
    def said(self) -> str:
        return self.message.said


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=PLAYER, user_id=PLAYER)
    )


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    """The cooldown store is module-level and survives between tests; a leftover
    entry would make /crea_scommessa refuse for reasons the test never set up."""
    cooldown.reset()
    yield
    cooldown.reset()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _open_event(
    session, *, title="Derby", window=None, status=EventStatus.open.value
) -> BettingEvent:
    event = await bet_svc.create_event(
        session,
        creator_tg_id=CREATOR,
        title=title,
        description="chi vince",
        options=[{"label": "Casa"}, {"label": "Trasferta"}],
        status=status,
        window_seconds=window,
    )
    await session.commit()
    return (
        await session.execute(
            select(BettingEvent)
            .where(BettingEvent.id == event.id)
            .options(selectinload(BettingEvent.options))
        )
    ).scalar_one()


async def _coins(session, tg_id: int) -> int:
    return (await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))).scalar_one()


async def _bets(session) -> list[UserBet]:
    return list((await session.execute(select(UserBet))).scalars().all())


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _invoke(handler, callback: _FakeCallback, *args) -> None:
    await handler(callback, callback.callback_data, *args)


# ---------------------------------------------------------------------------
# /scommesse — the list
# ---------------------------------------------------------------------------


class TestEventList:
    async def test_no_open_events_says_so_without_a_keyboard(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=100)
        message = _FakeMessage("/scommesse")

        await betting.cmd_scommesse(message, session, _state())

        assert "Nessuna scommessa aperta" in message.said
        assert message.markups == [None]

    async def test_in_a_group_it_redirects_instead_of_listing(self, session, user_factory):
        """The list marks which events the caller already bet on: answering in the
        group would publish that to everyone (§9)."""
        await user_factory(tg_id=PLAYER, coins=100)
        await _open_event(session)
        message = _FakeMessage("/scommesse", chat_type="supergroup")

        await betting.cmd_scommesse(message, session, _state())

        assert "privata" in message.said
        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=scommesse")

    async def test_an_event_already_played_is_marked(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        await bet_svc.place_bet(session, PLAYER, event.id, event.options[0].id, 100)
        await session.commit()
        message = _FakeMessage("/scommesse")

        await betting.cmd_scommesse(message, session, _state())

        marked = [b.text for row in message.markups[0].inline_keyboard for b in row]
        assert any(t.startswith("✅") for t in marked)

    async def test_opening_the_list_twice_deletes_the_first_prompt(self, session, user_factory):
        """Only one interactive betting message may be live per user, otherwise a
        stale keyboard stays tappable and bets land on the wrong screen."""
        await user_factory(tg_id=PLAYER, coins=1000)
        await _open_event(session)
        state = _state()
        message = _FakeMessage("/scommesse")

        await betting.show_events_private(message, session, state)
        first_id = (await state.get_data())["bet_active_msg_id"]
        await betting.show_events_private(message, session, state)

        assert message.bot.deleted == [(PLAYER, first_id)]
        assert (await state.get_data())["bet_active_msg_id"] != first_id

    async def test_a_delete_that_fails_does_not_stop_the_new_list(self, session, user_factory):
        """The old message may already be gone (user deleted it, Telegram expired
        it). That must not cost the user the command."""
        await user_factory(tg_id=PLAYER, coins=1000)
        await _open_event(session)
        state = _state()
        message = _FakeMessage("/scommesse", bot=_DeafBot())

        await betting.show_events_private(message, session, state)
        await betting.show_events_private(message, session, state)

        assert message.bot.deleted, "the delete must be attempted, not skipped"
        assert len(message.texts) == 2


# ---------------------------------------------------------------------------
# event:view — opening one event
# ---------------------------------------------------------------------------


class TestEventView:
    async def test_an_unknown_event_is_refused(self, session):
        callback = _FakeCallback(BetEventCb(action="view", event_id=999))

        await _invoke(betting.cb_event_view, callback, session, _state())

        assert callback.alerts and "non trovato" in callback.alerts[0]

    async def test_a_closed_event_shows_no_options(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.locked.value)
        callback = _FakeCallback(BetEventCb(action="view", event_id=event.id))

        await _invoke(betting.cb_event_view, callback, session, _state())

        assert "non accetta più puntate" in callback.said
        assert callback.message.markups == [None]

    async def test_an_open_event_shows_its_options_and_pool(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        await bet_svc.place_bet(session, PLAYER, event.id, event.options[0].id, 300)
        await session.commit()
        callback = _FakeCallback(BetEventCb(action="view", event_id=event.id))

        await _invoke(betting.cb_event_view, callback, session, _state())

        assert "300" in callback.said
        assert (
            _callbacks(callback.message.markups[0])[0]
            == BetOptionCb(action="pick", event_id=event.id, option_id=event.options[0].id).pack()
        )

    async def test_a_windowed_event_shows_its_deadline(self, session, user_factory):
        """A player must know the cutoff *before* choosing an amount — an event with
        an illimitata window has no such line at all."""
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, window=3600)
        callback = _FakeCallback(BetEventCb(action="view", event_id=event.id))

        await _invoke(betting.cb_event_view, callback, session, _state())

        assert "Chiude alle" in callback.said

    async def test_an_unlimited_event_shows_no_deadline(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, window=None)
        callback = _FakeCallback(BetEventCb(action="view", event_id=event.id))

        await _invoke(betting.cb_event_view, callback, session, _state())

        assert "Chiude alle" not in callback.said


# ---------------------------------------------------------------------------
# bet_option / bet_amount — choosing what and how much
# ---------------------------------------------------------------------------


class TestOptionAndAmount:
    async def test_betting_on_a_closed_event_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.locked.value)
        callback = _FakeCallback(
            BetOptionCb(action="pick", event_id=event.id, option_id=event.options[0].id)
        )

        await _invoke(betting.cb_bet_option, callback, session)

        assert callback.alerts and "non disponibile" in callback.alerts[0]

    async def test_an_option_from_another_event_is_refused(self, session, user_factory):
        """The option id travels in callback_data, so it is user-supplied: it must be
        checked against *this* event, not merely parsed."""
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        other = await _open_event(session, title="Altra")
        callback = _FakeCallback(
            BetOptionCb(action="pick", event_id=event.id, option_id=other.options[0].id)
        )

        await _invoke(betting.cb_bet_option, callback, session)

        assert callback.alerts and "Opzione non trovata" in callback.alerts[0]

    async def test_the_amount_keyboard_only_offers_what_the_player_can_afford(
        self, session, user_factory
    ):
        await user_factory(tg_id=PLAYER, coins=120)
        event = await _open_event(session)
        callback = _FakeCallback(
            BetOptionCb(action="pick", event_id=event.id, option_id=event.options[0].id)
        )

        await _invoke(betting.cb_bet_option, callback, session)

        amounts = [
            c for c in _callbacks(callback.message.markups[0]) if c.startswith("bet_amount:")
        ]
        assert [c.rsplit(":", 1)[1] for c in amounts] == ["50", "100"]
        assert "120" in callback.said

    async def test_a_player_with_no_wallet_is_shown_zero(self, session):
        """`cb_bet_option` is reachable before the user ever has a Wallet row (a
        keyboard forwarded from someone else). Zero, not a crash."""
        event = await _open_event(session)
        callback = _FakeCallback(
            BetOptionCb(action="pick", event_id=event.id, option_id=event.options[0].id)
        )

        await _invoke(betting.cb_bet_option, callback, session)

        assert "0 🪙" in callback.said

    @pytest.mark.parametrize("amount", [0, -100])
    async def test_a_non_positive_amount_is_refused_on_a_real_event(
        self, session, user_factory, amount
    ):
        """On a *real* open event, so the refusal can only come from the amount
        check: pointing this at a non-existent event would pass even with the
        check deleted, because 'event not available' is also an alert."""
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        callback = _FakeCallback(
            BetAmountCb(
                action="pick", event_id=event.id, option_id=event.options[0].id, amount=amount
            )
        )

        await _invoke(betting.cb_bet_amount, callback, session, _state())

        assert callback.alerts and "positivo" in callback.alerts[0]

    async def test_a_preset_amount_leads_to_the_confirm_button(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        option = event.options[0]
        callback = _FakeCallback(
            BetAmountCb(action="pick", event_id=event.id, option_id=option.id, amount=100)
        )

        await _invoke(betting.cb_bet_amount, callback, session, _state())

        assert BetConfirmCb(
            action="place", event_id=event.id, option_id=option.id, amount=100
        ).pack() in _callbacks(callback.message.markups[0])

    async def test_the_payout_estimate_is_the_whole_pool_for_the_only_bettor(
        self, session, user_factory
    ):
        """Twitch-style: alone on the winning side you get everything on the table,
        so 100 staked against 300 already on the other option shows ~400."""
        await user_factory(tg_id=PLAYER, coins=1000)
        await user_factory(tg_id=PLAYER + 1, coins=1000)
        event = await _open_event(session)
        await bet_svc.place_bet(session, PLAYER + 1, event.id, event.options[1].id, 300)
        await session.commit()
        callback = _FakeCallback(
            BetAmountCb(action="pick", event_id=event.id, option_id=event.options[0].id, amount=100)
        )

        await _invoke(betting.cb_bet_amount, callback, session, _state())

        assert "400" in callback.said

    async def test_confirming_an_amount_on_a_closed_event_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.locked.value)
        callback = _FakeCallback(
            BetAmountCb(action="pick", event_id=event.id, option_id=event.options[0].id, amount=100)
        )

        await _invoke(betting.cb_bet_amount, callback, session, _state())

        assert callback.alerts and "non disponibile" in callback.alerts[0]

    async def test_confirming_an_unknown_option_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        callback = _FakeCallback(
            BetAmountCb(action="pick", event_id=event.id, option_id=9999, amount=100)
        )

        await _invoke(betting.cb_bet_amount, callback, session, _state())

        assert callback.alerts and "Opzione non trovata" in callback.alerts[0]


# ---------------------------------------------------------------------------
# The custom amount (free text)
# ---------------------------------------------------------------------------


class TestCustomAmount:
    async def test_the_custom_button_arms_the_fsm_with_event_and_option(self, session):
        callback = _FakeCallback(BetCustomCb(action="open", event_id=7, option_id=9))
        state = _state()

        await _invoke(betting.cb_bet_custom, callback, state)

        data = await state.get_data()
        assert (data["custom_bet_event"], data["custom_bet_option"]) == (7, 9)
        assert await state.get_state() == betting.BetCustomAmountState.waiting_for_amount.state

    async def test_a_non_numeric_amount_keeps_the_player_in_the_fsm(self, session):
        state = _state()
        await state.set_state(betting.BetCustomAmountState.waiting_for_amount)
        await state.update_data(custom_bet_event=1, custom_bet_option=1)
        message = _FakeMessage("tantissimo")

        await betting.fsm_custom_amount(message, state, session)

        assert "numero intero" in message.said
        assert await state.get_state() == betting.BetCustomAmountState.waiting_for_amount.state

    async def test_an_expired_session_is_told_to_restart(self, session):
        """The FSM state survives a restart, its data does not: without the stored
        event id the amount could only be applied to a guess."""
        state = _state()
        await state.set_state(betting.BetCustomAmountState.waiting_for_amount)
        message = _FakeMessage("100")

        await betting.fsm_custom_amount(message, state, session)

        assert "Sessione scaduta" in message.said
        assert await state.get_state() is None

    async def test_an_event_closed_while_typing_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.locked.value)
        state = _state()
        await state.update_data(custom_bet_event=event.id, custom_bet_option=event.options[0].id)
        message = _FakeMessage("100")

        await betting.fsm_custom_amount(message, state, session)

        assert "Evento non disponibile" in message.said
        assert await state.get_state() is None

    async def test_an_unknown_option_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        state = _state()
        await state.update_data(custom_bet_event=event.id, custom_bet_option=9999)
        message = _FakeMessage("100")

        await betting.fsm_custom_amount(message, state, session)

        assert "Opzione non trovata" in message.said

    async def test_a_valid_custom_amount_reaches_the_same_confirm_button(
        self, session, user_factory
    ):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        option = event.options[0]
        state = _state()
        await state.update_data(custom_bet_event=event.id, custom_bet_option=option.id)
        message = _FakeMessage("175")

        await betting.fsm_custom_amount(message, state, session)

        assert BetConfirmCb(
            action="place", event_id=event.id, option_id=option.id, amount=175
        ).pack() in _callbacks(message.markups[-1])
        assert await state.get_state() is None


# ---------------------------------------------------------------------------
# bet_confirm — the only handler in this file that moves money
# ---------------------------------------------------------------------------


class TestConfirm:
    async def test_a_confirmed_bet_debits_exactly_the_amount(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        option = event.options[0]
        callback = _FakeCallback(
            BetConfirmCb(action="place", event_id=event.id, option_id=option.id, amount=250)
        )

        await _invoke(betting.cb_bet_confirm, callback, session, _state())

        assert await _coins(session, PLAYER) == 750
        placed = await _bets(session)
        assert len(placed) == 1
        assert (placed[0].amount, placed[0].option_id) == (250, option.id)
        assert "Scommessa piazzata" in callback.said

    async def test_the_confirm_message_announces_the_participation_xp(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        callback = _FakeCallback(
            BetConfirmCb(
                action="place", event_id=event.id, option_id=event.options[0].id, amount=100
            )
        )

        await _invoke(betting.cb_bet_confirm, callback, session, _state())

        assert (f"+{settings.xp_per_bet_placed} XP" in callback.said) == (
            settings.xp_per_bet_placed > 0
        )

    async def test_pressing_confirm_twice_debits_once(self, session, user_factory):
        """The keyboard stays on screen after the first tap, so the second tap is a
        normal thing for a player to do — not an attack."""
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        data = BetConfirmCb(
            action="place", event_id=event.id, option_id=event.options[0].id, amount=250
        )

        await _invoke(betting.cb_bet_confirm, _FakeCallback(data), session, _state())
        second = _FakeCallback(data)
        await _invoke(betting.cb_bet_confirm, second, session, _state())

        assert second.alerts and "già scommesso" in second.alerts[0]
        assert await _coins(session, PLAYER) == 750
        assert len(await _bets(session)) == 1

    async def test_a_bet_on_a_locked_event_takes_no_money(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.locked.value)
        callback = _FakeCallback(
            BetConfirmCb(
                action="place", event_id=event.id, option_id=event.options[0].id, amount=250
            )
        )

        await _invoke(betting.cb_bet_confirm, callback, session, _state())

        assert callback.alerts and "non accetta più puntate" in callback.alerts[0]
        assert await _coins(session, PLAYER) == 1000

    async def test_a_bet_on_a_deleted_event_takes_no_money(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        callback = _FakeCallback(
            BetConfirmCb(action="place", event_id=999, option_id=1, amount=250)
        )

        await _invoke(betting.cb_bet_confirm, callback, session, _state())

        assert callback.alerts and "Evento non trovato" in callback.alerts[0]
        assert await _coins(session, PLAYER) == 1000

    async def test_an_unaffordable_bet_says_the_numbers_and_takes_nothing(
        self, session, user_factory
    ):
        await user_factory(tg_id=PLAYER, coins=100)
        event = await _open_event(session)
        callback = _FakeCallback(
            BetConfirmCb(
                action="place", event_id=event.id, option_id=event.options[0].id, amount=250
            )
        )

        await _invoke(betting.cb_bet_confirm, callback, session, _state())

        assert callback.alerts and "100" in callback.alerts[0] and "250" in callback.alerts[0]
        assert await _coins(session, PLAYER) == 100
        assert await _bets(session) == []

    async def test_a_trophy_announcement_that_fails_does_not_undo_the_bet(
        self, seeded_session, user_factory
    ):
        """The announcement happens after the commit. Injecting the failure in the
        bot (where it really occurs) rather than stubbing `announce_trophies` —
        stubbing it would only test the stub, since it swallows errors by design."""
        session = seeded_session
        await user_factory(tg_id=PLAYER, coins=5000)  # ≥1000 → unlocks a balance trophy
        event = await _open_event(session)
        bot = _DeafBot()
        message = _FakeMessage(bot=bot)
        callback = _FakeCallback(
            BetConfirmCb(
                action="place", event_id=event.id, option_id=event.options[0].id, amount=250
            ),
            message=message,
        )
        group_registry.set_runtime_group_id(-100_555)
        try:
            await _invoke(betting.cb_bet_confirm, callback, session, _state())
        finally:
            group_registry.set_runtime_group_id(None)

        assert bot.sent, "the announcement must be attempted, or this asserts nothing"
        assert await _coins(session, PLAYER) == 4750
        assert len(await _bets(session)) == 1


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestNavigation:
    async def test_back_returns_to_the_list(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        callback = _FakeCallback(BetCb(action="back"))

        await _invoke(betting.cb_bet_back, callback, session, _state())

        assert BetEventCb(action="view", event_id=event.id).pack() in _callbacks(
            callback.message.markups[0]
        )

    async def test_back_with_nothing_left_open_says_so(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        callback = _FakeCallback(BetCb(action="back"))

        await _invoke(betting.cb_bet_back, callback, session, _state())

        assert "Nessuna scommessa aperta" in callback.said

    async def test_close_deletes_the_prompt_and_forgets_it(self, session):
        state = _state()
        await state.update_data(bet_active_msg_id=42)
        callback = _FakeCallback(BetCb(action="close"))

        await _invoke(betting.cb_bet_close, callback, state)

        assert callback.message.deleted
        assert (await state.get_data())["bet_active_msg_id"] is None

    async def test_close_still_forgets_the_prompt_if_the_delete_fails(self, session):
        """Otherwise the tracked id stays forever and every later prompt tries to
        delete the same dead message."""
        state = _state()
        await state.update_data(bet_active_msg_id=42)
        callback = _FakeCallback(BetCb(action="close"), message=_UndeletableMessage())

        await _invoke(betting.cb_bet_close, callback, state)

        assert (await state.get_data())["bet_active_msg_id"] is None


# ---------------------------------------------------------------------------
# Deep-link entry points (from common.cmd_start)
# ---------------------------------------------------------------------------


class TestDeepLinks:
    async def test_a_bet_deep_link_opens_the_options(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session)
        state = _state()
        message = _FakeMessage()

        await betting.start_bet_view(message, session, event.id, state)

        assert BetOptionCb(
            action="pick", event_id=event.id, option_id=event.options[0].id
        ).pack() in _callbacks(message.markups[0])
        assert (await state.get_data())["bet_active_msg_id"] is not None

    async def test_a_deep_link_to_a_closed_event_is_refused(self, session, user_factory):
        await user_factory(tg_id=PLAYER, coins=1000)
        event = await _open_event(session, status=EventStatus.resolved.value)
        message = _FakeMessage()

        await betting.start_bet_view(message, session, event.id, _state())

        assert "non trovata o non più disponibile" in message.said

    async def test_a_deep_link_to_a_missing_event_is_refused(self, session):
        message = _FakeMessage()

        await betting.start_bet_view(message, session, 999, _state())

        assert "non trovata o non più disponibile" in message.said

    async def test_the_custom_amount_deep_link_arms_the_fsm(self, session):
        state = _state()
        message = _FakeMessage()

        await betting.start_custom_amount(message, state, event_id=3, option_id=4)

        data = await state.get_data()
        assert (data["custom_bet_event"], data["custom_bet_option"]) == (3, 4)
        assert await state.get_state() == betting.BetCustomAmountState.waiting_for_amount.state
        assert data["bet_active_msg_id"] is not None


# ---------------------------------------------------------------------------
# Creation FSM
# ---------------------------------------------------------------------------


class TestCreationEntry:
    async def test_in_a_group_it_sends_a_link_instead_of_starting_the_fsm(self, session):
        """A multi-step FSM in a group would interleave with everyone else's
        messages, so creation only ever happens in private."""
        message = _FakeMessage("/crea_scommessa", chat_type="supergroup")
        state = _state()

        await betting.cmd_crea_scommessa(message, state)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=create_bet")
        assert await state.get_state() is None

    async def test_in_private_it_asks_for_the_title(self, session):
        message = _FakeMessage("/crea_scommessa")
        state = _state()

        await betting.cmd_crea_scommessa(message, state)

        assert await state.get_state() == betting.BetCreationStates.waiting_for_title.state
        assert "Step 1/4" in message.said

    async def test_the_second_attempt_within_the_cooldown_is_refused(self, session):
        """Creation is not admin-only, so without the cooldown one user could fill
        the event list. Admins are *not* exempt here (exempt_admin=False)."""
        state = _state()
        await betting.cmd_crea_scommessa(_FakeMessage("/crea_scommessa"), state)
        second = _FakeMessage("/crea_scommessa")

        await betting.cmd_crea_scommessa(second, state)

        assert "Vai più piano" in second.said
        assert "Step 1/4" not in second.said

    async def test_the_draft_entry_point_announces_it_is_a_draft(self, session):
        message = _FakeMessage()
        state = _state()

        await betting.start_bet_creation(message, state, as_draft=True)

        assert "bozza" in message.said
        assert (await state.get_data())["bet_as_draft"] is True

    async def test_the_direct_entry_point_does_not(self, session):
        message = _FakeMessage()
        state = _state()

        await betting.start_bet_creation(message, state)

        assert "bozza" not in message.said
        assert (await state.get_data())["bet_as_draft"] is False


class TestCreationCancel:
    async def test_cancel_outside_the_fsm_is_a_no_op(self, session):
        """The button lives on a message that stays on screen after the FSM ended:
        tapping it then must not pop a confirmation for nothing."""
        callback = _FakeCallback(BetCb(action="cancel_creation"))

        await _invoke(betting.cb_cancel_creation, callback, _state())

        assert callback.message.texts == []

    async def test_cancel_inside_the_fsm_asks_first(self, session):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_title)
        callback = _FakeCallback(BetCb(action="cancel_creation"))

        await _invoke(betting.cb_cancel_creation, callback, state)

        assert "Sicuro" in callback.said
        assert set(_callbacks(callback.message.markups[0])) == {
            BetCb(action="cancel_yes").pack(),
            BetCb(action="cancel_no").pack(),
        }
        assert await state.get_state() is not None, "asking is not cancelling"

    async def test_confirming_the_cancel_clears_the_fsm(self, session):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_title)
        await state.update_data(title="mezzo lavoro")
        callback = _FakeCallback(BetCb(action="cancel_yes"))

        await _invoke(betting.cb_cancel_creation_yes, callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    async def test_refusing_the_cancel_keeps_everything(self, session):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_description)
        await state.update_data(title="mezzo lavoro")
        callback = _FakeCallback(BetCb(action="cancel_no"))

        await _invoke(betting.cb_cancel_creation_no, callback)

        assert await state.get_state() == betting.BetCreationStates.waiting_for_description.state
        assert (await state.get_data())["title"] == "mezzo lavoro"


class TestCreationSteps:
    async def test_a_too_short_title_does_not_advance(self, session):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_title)
        message = _FakeMessage("ab")

        await betting.fsm_bet_title(message, state)

        assert "almeno 4 caratteri" in message.said
        assert await state.get_state() == betting.BetCreationStates.waiting_for_title.state

    async def test_a_long_title_is_truncated_not_refused(self, session):
        state = _state()
        message = _FakeMessage("x" * 500)

        await betting.fsm_bet_title(message, state)

        assert len((await state.get_data())["title"]) == 200

    async def test_a_too_short_description_does_not_advance(self, session):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_description)
        message = _FakeMessage("no")

        await betting.fsm_bet_description(message, state)

        assert "almeno 4 caratteri" in message.said
        assert await state.get_state() == betting.BetCreationStates.waiting_for_description.state

    async def test_a_valid_description_moves_on_to_the_options(self, session):
        state = _state()
        message = _FakeMessage("partita di cartello")

        await betting.fsm_bet_description(message, state)

        assert await state.get_state() == betting.BetCreationStates.waiting_for_options.state
        assert (await state.get_data())["description"] == "partita di cartello"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("solo una", "almeno 2 opzioni"),
            ("\n".join(f"opt{i}" for i in range(9)), "Massimo 8 opzioni"),
            ("valida\n" + "x" * 101, "max 100 caratteri"),
        ],
    )
    async def test_bad_option_lists_are_refused(self, session, raw, expected):
        state = _state()
        await state.set_state(betting.BetCreationStates.waiting_for_options)
        message = _FakeMessage(raw)

        await betting.fsm_bet_options(message, state)

        assert expected in message.said
        assert await state.get_state() == betting.BetCreationStates.waiting_for_options.state


class TestCreationWindow:
    async def _armed(self, state: FSMContext) -> None:
        await state.set_state(betting.BetCreationStates.waiting_for_window)
        await state.update_data(
            title="Derby", description="chi vince", options=["Casa", "Trasferta"]
        )

    async def test_a_window_without_seconds_leaves_the_armed_fsm_unchanged(
        self, session, user_factory
    ):
        """A typed optional field is not a valid unlimited-window choice.

        Removing the ``seconds is None`` guard would create an unlimited event,
        clear this armed FSM and send a confirmation before the user chose a
        duration.  The callback answer itself is intentionally still sent to
        stop Telegram's spinner.
        """
        await user_factory(tg_id=CREATOR, username="creator")
        state = _state()
        await self._armed(state)
        state_before = await state.get_data()
        callback = _FakeCallback(BetCb(action="window"), user_id=CREATOR)

        await _invoke(betting.cb_bet_window, callback, state, session)

        assert (await session.execute(select(BettingEvent))).scalars().all() == []
        assert (await session.execute(select(ScheduledTask))).scalars().all() == []
        assert callback.message.texts == []
        assert await state.get_state() == betting.BetCreationStates.waiting_for_window.state
        assert await state.get_data() == state_before
        assert callback.answers == [(None, False)]

    async def test_a_preset_window_sets_a_deadline_and_schedules_the_lock(
        self, session, user_factory
    ):
        """The window is only real if something closes it: the event carries the
        deadline *and* a scheduled task, otherwise it stays open forever."""
        await user_factory(tg_id=CREATOR, username="creator")
        state = _state()
        await self._armed(state)
        callback = _FakeCallback(BetCb(action="window", seconds=3600), user_id=CREATOR)

        await _invoke(betting.cb_bet_window, callback, state, session)

        event = (await session.execute(select(BettingEvent))).scalar_one()
        assert event.status == EventStatus.open.value
        assert event.closes_at is not None
        task = (await session.execute(select(ScheduledTask))).scalar_one()
        assert (task.task_type, task.ref_id) == ("bet", event.id)
        assert await state.get_state() is None

    async def test_an_unlimited_window_schedules_nothing(self, session, user_factory):
        await user_factory(tg_id=CREATOR, username="creator")
        state = _state()
        await self._armed(state)
        callback = _FakeCallback(BetCb(action="window", seconds=0), user_id=CREATOR)

        await _invoke(betting.cb_bet_window, callback, state, session)

        event = (await session.execute(select(BettingEvent))).scalar_one()
        assert event.closes_at is None
        assert (await session.execute(select(ScheduledTask))).scalars().all() == []
        assert "illimitata" in callback.said

    async def test_the_custom_button_asks_for_a_duration(self, session, user_factory):
        state = _state()
        await self._armed(state)
        callback = _FakeCallback(BetCb(action="window_custom"), user_id=CREATOR)

        await _invoke(betting.cb_bet_window, callback, state, session)

        assert await state.get_state() == betting.BetCreationStates.waiting_for_window_custom.state
        assert (await session.execute(select(BettingEvent))).scalars().all() == [], (
            "nothing may be created until the duration is known"
        )

    async def test_an_unparseable_duration_creates_nothing(self, session, user_factory):
        state = _state()
        await self._armed(state)
        await state.set_state(betting.BetCreationStates.waiting_for_window_custom)
        message = _FakeMessage("domani forse", user_id=CREATOR)

        await betting.fsm_bet_window_custom(message, state, session)

        assert (await session.execute(select(BettingEvent))).scalars().all() == []
        assert await state.get_state() == betting.BetCreationStates.waiting_for_window_custom.state

    async def test_a_valid_custom_duration_creates_the_event(self, session, user_factory):
        await user_factory(tg_id=CREATOR, username="creator")
        state = _state()
        await self._armed(state)
        await state.set_state(betting.BetCreationStates.waiting_for_window_custom)
        message = _FakeMessage("2h", user_id=CREATOR)

        await betting.fsm_bet_window_custom(message, state, session)

        event = (await session.execute(select(BettingEvent))).scalar_one()
        assert event.betting_window_seconds == 7200
        assert "Scommessa creata" in message.said

    async def test_a_draft_is_created_closed_and_unscheduled(self, session, user_factory):
        """A draft is activated later from the Events hub — arming its auto-lock now
        would close the betting window before anyone could bet."""
        await user_factory(tg_id=CREATOR, username="creator")
        state = _state()
        await self._armed(state)
        await state.update_data(bet_as_draft=True)
        callback = _FakeCallback(BetCb(action="window", seconds=3600), user_id=CREATOR)

        await _invoke(betting.cb_bet_window, callback, state, session)

        event = (await session.execute(select(BettingEvent))).scalar_one()
        assert event.status == EventStatus.draft.value
        assert (await session.execute(select(ScheduledTask))).scalars().all() == []
        assert "bozza" in callback.said
