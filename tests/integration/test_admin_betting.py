"""Integration tests for `handlers/admin_betting.py` — the admin panel that settles bets.

This file existed at 21% covered, which was the lowest number in the project and also
the worst place for it: these handlers are where an admin turns a bet into money. The
service layer underneath (`bet_service`) is well tested; what was untested is the layer
that decides *when* to call it, what to tell the players afterwards, and what happens
when the same button is pressed twice.

The handlers are driven directly with stubs, the same way `test_quiz_try.py` does it —
no aiogram dispatcher, no network. What the tests assert is deliberately not the
wording of the messages (that would freeze copy nobody agreed to freeze) but the three
things that actually matter:

  * **money**: wallets and the ledger after a resolve / a cancel;
  * **the guards**: pressing confirm twice must not pay twice;
  * **best-effort really is best-effort**: a player who blocked the bot must not be
    able to abort the settlement of everyone else's bet.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import services.bet_service as bet_svc
from database.models import BettingEvent, EventStatus, Wallet
from handlers import admin_betting as ab
from handlers.callbacks import AdminBetCb

ADMIN_ID = 1


class _FakeBot:
    """Records the private DMs the panel sends to players."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _BlockedBot(_FakeBot):
    """Every DM fails — a player who blocked the bot, or never opened it."""

    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("Forbidden: bot was blocked by the user")


class _FakeMessage:
    def __init__(self, bot=None) -> None:
        self.bot = bot or _FakeBot()
        self.chat = SimpleNamespace(id=ADMIN_ID, type="private")
        self.from_user = SimpleNamespace(id=ADMIN_ID)
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def delete(self):
        self.deleted = True


class _FakeCallback:
    def __init__(self, callback_data: AdminBetCb, message=None, user_id=ADMIN_ID) -> None:
        self.data = callback_data.pack()
        self.callback_data = callback_data
        self.message = message or _FakeMessage()
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]


def _admin_bet(
    action: str, event_id: int | None = None, option_id: int | None = None
) -> AdminBetCb:
    return AdminBetCb(action=action, event_id=event_id, option_id=option_id)


async def _invoke(handler, callback: _FakeCallback, session) -> None:
    await handler(callback, callback.callback_data, session)


async def _event_with_bets(session, user_factory, *, amounts=((10, 0, 300), (11, 1, 100))):
    """An open event plus one bet each. `amounts` is (tg_id, option_index, amount)."""
    await user_factory(tg_id=ADMIN_ID, username="admin")
    event = await bet_svc.create_event(
        session, creator_tg_id=ADMIN_ID, title="Chi vince?", description="derby",
        options=[{"label": "Casa"}, {"label": "Trasferta"}],
    )
    await session.commit()
    event = (await session.execute(
        select(BettingEvent).where(BettingEvent.id == event.id)
        .options(selectinload(BettingEvent.options))
    )).scalar_one()

    for tg_id, option_index, amount in amounts:
        await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=1000)
        await bet_svc.place_bet(
            session, user_tg_id=tg_id, event_id=event.id,
            option_id=event.options[option_index].id, amount=amount,
        )
        await session.commit()
    return event


async def _coins(session, tg_id: int) -> int:
    """Column select: an entity select could be answered from the identity map with
    the balance this test already had in memory (STEERING §5)."""
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _status(session, event_id: int) -> str:
    return (
        await session.execute(select(BettingEvent.status).where(BettingEvent.id == event_id))
    ).scalar_one()


@pytest.mark.parametrize(
    ("handler", "callback_data"),
    [
        (ab.cb_admin_event, _admin_bet("event")),
        (ab.cb_admin_lock, _admin_bet("lock")),
        (ab.cb_admin_confirm_lock, _admin_bet("confirm_lock")),
        (ab.cb_admin_resolve, _admin_bet("resolve")),
        (ab.cb_admin_pick_winner, _admin_bet("pick_winner", event_id=1)),
        (ab.cb_admin_confirm_resolve, _admin_bet("confirm_resolve", event_id=1)),
        (ab.cb_admin_cancel, _admin_bet("cancel")),
        (ab.cb_admin_confirm_cancel, _admin_bet("confirm_cancel")),
    ],
)
async def test_handlers_reject_typed_callbacks_missing_required_ids(
    handler, callback_data, session
):
    cb = _FakeCallback(callback_data)

    await _invoke(handler, cb, session)

    assert cb.alerts == ["❌ Dati callback non validi."]


class TestResolve:
    async def test_the_whole_pot_reaches_the_winners(self, session, user_factory):
        """300 + 100 in, 400 out, all of it to the one winner.

        Balances are read from the database, not from the summary the handler built:
        the summary is what the admin is *told*, and the point of the test is that the
        two agree.
        """
        event = await _event_with_bets(session, user_factory)
        winner_option = event.options[0].id
        cb = _FakeCallback(_admin_bet("confirm_resolve", event.id, winner_option))

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)

        assert await _status(session, event.id) == EventStatus.resolved.value
        # 1000 - 300 staked, + 400 pot back
        assert await _coins(session, 10) == 1100
        assert await _coins(session, 11) == 900   # staked 100, lost it
        assert 1100 + 900 == 2000, "the two players started with 1000 each"

    async def test_both_sides_are_told(self, session, user_factory):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("confirm_resolve", event.id, event.options[0].id))

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)

        told = {tg_id: text for tg_id, text in cb.message.bot.sent}
        assert "Hai vinto" in told[10]
        assert "400" in told[10], "the winner must be told the payout, not just that they won"
        assert "persa" in told[11]

    async def test_confirming_twice_does_not_pay_twice(self, session, user_factory):
        """The one that matters. The admin's connection drops, they tap again.

        `resolve_event` raises `EventAlreadySettledError` the second time and the
        handler turns it into an alert — but the assertion here is on the wallet, not
        on the alert, because the alert is cosmetic and the balance is not.
        """
        event = await _event_with_bets(session, user_factory)
        data = _admin_bet("confirm_resolve", event.id, event.options[0].id)

        await _invoke(ab.cb_admin_confirm_resolve, _FakeCallback(data), session)
        after_first = await _coins(session, 10)

        second = _FakeCallback(data)
        await _invoke(ab.cb_admin_confirm_resolve, second, session)

        assert await _coins(session, 10) == after_first, "the pot was paid out twice"
        assert second.alerts and "già chiuso" in second.alerts[0]
        assert second.message.bot.sent == [], "players were notified a second time"

    async def test_a_blocked_player_does_not_abort_the_settlement(
        self, session, user_factory
    ):
        """The DMs are best-effort by design, and this is what that has to mean: a
        player who blocked the bot cannot leave the event unresolved for everybody
        else. Without the `try/except` around each send, the first failure would
        propagate out of the handler *after* the commit — money moved, admin sees an
        error, event looks stuck."""
        event = await _event_with_bets(session, user_factory)
        message = _FakeMessage(bot=_BlockedBot())
        cb = _FakeCallback(
            _admin_bet("confirm_resolve", event.id, event.options[0].id), message
        )

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)  # must not raise

        assert await _status(session, event.id) == EventStatus.resolved.value
        assert await _coins(session, 10) == 1100
        assert message.texts, "the admin was left without a confirmation screen"

    async def test_winners_are_checked_for_trophies(
        self, session, user_factory, monkeypatch
    ):
        """Winning a bet can unlock a milestone, and the announcement is part of the
        settlement rather than a later job — so it has to happen for each winner and
        for nobody else."""
        checked: list[int] = []
        announced: list[int] = []

        async def fake_check(db, tg_id):
            checked.append(tg_id)
            return ["primo_colpo"]

        async def fake_announce(bot, db, tg_id, milestones):
            announced.append(tg_id)

        monkeypatch.setattr(ab.badge_service, "check_and_award_milestones", fake_check)
        monkeypatch.setattr(ab, "announce_trophies", fake_announce)

        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("confirm_resolve", event.id, event.options[0].id))

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)

        assert checked == [10], "the loser was checked for winner milestones"
        assert announced == [10]

    async def test_a_failing_trophy_announce_does_not_undo_the_payout(
        self, session, user_factory, monkeypatch
    ):
        """The trophy block is wrapped in a bare `except` on purpose. This pins why:
        the money has already been committed by the time it runs, so letting it
        propagate would show the admin a failure for a payout that did happen — and
        invite them to press confirm again.
        """
        async def boom(db, tg_id):
            raise RuntimeError("badge catalog unavailable")

        monkeypatch.setattr(ab.badge_service, "check_and_award_milestones", boom)

        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("confirm_resolve", event.id, event.options[0].id))

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)  # must not raise

        assert await _coins(session, 10) == 1100
        assert cb.message.texts, "the admin was left without a confirmation screen"

    async def test_a_missing_event_is_an_alert_not_a_crash(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        cb = _FakeCallback(_admin_bet("confirm_resolve", 999999, 1))

        await _invoke(ab.cb_admin_confirm_resolve, cb, session)

        assert cb.alerts and "non trovato" in cb.alerts[0]


class TestPayoutPreview:
    async def test_the_preview_shows_the_numbers_the_admin_is_about_to_approve(
        self, session, user_factory
    ):
        """This screen is the last thing between the admin and an irreversible payout.
        If it under-reports the pot, the admin approves something else than they read.
        """
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("pick_winner", event.id, event.options[0].id))

        await _invoke(ab.cb_admin_pick_winner, cb, session)

        text = cb.message.texts[-1]
        assert "400" in text, "total pot missing from the confirmation screen"
        assert "Casa" in text, "the winning option must be named, not just its id"

    async def test_an_unknown_option_is_refused(self, session, user_factory):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("pick_winner", event.id, 999999))

        await _invoke(ab.cb_admin_pick_winner, cb, session)

        assert cb.alerts and "Opzione non trovata" in cb.alerts[0]

    async def test_a_missing_event_is_refused(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        cb = _FakeCallback(_admin_bet("pick_winner", 999999, 1))

        await _invoke(ab.cb_admin_pick_winner, cb, session)

        assert cb.alerts and "non trovato" in cb.alerts[0]

    async def test_a_long_winner_list_is_truncated(self, session, user_factory):
        """Telegram rejects messages over 4096 characters. A popular bet has more
        winners than fit, so the preview lists the first eight and counts the rest —
        without that, the admin would get an API error instead of the screen they
        need in order to pay anyone at all.
        """
        bets = tuple((100 + i, 0, 10) for i in range(11))
        event = await _event_with_bets(session, user_factory, amounts=bets)
        cb = _FakeCallback(_admin_bet("pick_winner", event.id, event.options[0].id))

        await _invoke(ab.cb_admin_pick_winner, cb, session)

        text = cb.message.texts[-1]
        assert "e altri 3 vincitori" in text
        assert text.count("payout") == 8

    async def test_the_preview_says_so_when_nobody_backed_the_option(
        self, session, user_factory
    ):
        """Declaring an option nobody bet on is legal — the whole pot then has no
        winners. The admin has to be able to see that *before* confirming."""
        event = await _event_with_bets(session, user_factory, amounts=((10, 0, 300),))
        cb = _FakeCallback(_admin_bet("pick_winner", event.id, event.options[1].id))

        await _invoke(ab.cb_admin_pick_winner, cb, session)

        assert "Nessuna scommessa su questa opzione" in cb.message.texts[-1]


class TestCancel:
    async def test_every_pending_bet_is_refunded_exactly_once(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("confirm_cancel", event.id))

        await _invoke(ab.cb_admin_confirm_cancel, cb, session)

        assert await _status(session, event.id) == EventStatus.cancelled.value
        assert await _coins(session, 10) == 1000, "refund did not restore the stake"
        assert await _coins(session, 11) == 1000
        assert {tg_id for tg_id, _ in cb.message.bot.sent} == {10, 11}

    async def test_cancelling_twice_does_not_refund_twice(self, session, user_factory):
        event = await _event_with_bets(session, user_factory)
        data = _admin_bet("confirm_cancel", event.id)

        await _invoke(ab.cb_admin_confirm_cancel, _FakeCallback(data), session)
        second = _FakeCallback(data)
        await _invoke(ab.cb_admin_confirm_cancel, second, session)

        assert await _coins(session, 10) == 1000, "the stake was credited back twice"
        assert second.alerts and "già chiuso" in second.alerts[0]

    async def test_a_blocked_player_does_not_stop_the_other_refunds(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        message = _FakeMessage(bot=_BlockedBot())
        cb = _FakeCallback(_admin_bet("confirm_cancel", event.id), message)

        await _invoke(ab.cb_admin_confirm_cancel, cb, session)  # must not raise

        assert await _coins(session, 10) == 1000
        assert await _coins(session, 11) == 1000

    async def test_cancelling_a_missing_event_is_an_alert(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        cb = _FakeCallback(_admin_bet("confirm_cancel", 999999))

        await _invoke(ab.cb_admin_confirm_cancel, cb, session)

        assert cb.alerts and "non trovato" in cb.alerts[0]

    async def test_the_confirmation_screen_states_what_will_be_refunded(
        self, session, user_factory
    ):
        """An irreversible refund is being approved from this screen; the amount on it
        has to be the amount that moves."""
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("cancel", event.id))

        await _invoke(ab.cb_admin_cancel, cb, session)

        assert "400" in cb.message.texts[-1]

    async def test_an_already_settled_event_cannot_reach_the_cancel_screen(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        resolved = _FakeCallback(
            _admin_bet("confirm_resolve", event.id, event.options[0].id)
        )
        await _invoke(ab.cb_admin_confirm_resolve, resolved, session)
        cb = _FakeCallback(_admin_bet("cancel", event.id))

        await _invoke(ab.cb_admin_cancel, cb, session)

        assert cb.alerts and "già chiuso" in cb.alerts[0]
        assert cb.message.texts == [], "a dead-end screen was rendered anyway"


class TestLock:
    async def test_locking_stops_new_bets_and_keeps_the_placed_ones(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("confirm_lock", event.id))

        await _invoke(ab.cb_admin_confirm_lock, cb, session)

        assert await _status(session, event.id) == EventStatus.locked.value
        assert await _coins(session, 10) == 700, "locking must not touch the stakes"

    async def test_locking_a_resolved_event_is_refused(self, session, user_factory):
        event = await _event_with_bets(session, user_factory)
        resolved = _FakeCallback(
            _admin_bet("confirm_resolve", event.id, event.options[0].id)
        )
        await _invoke(ab.cb_admin_confirm_resolve, resolved, session)
        cb = _FakeCallback(_admin_bet("confirm_lock", event.id))

        await _invoke(ab.cb_admin_confirm_lock, cb, session)

        assert cb.alerts and "già chiuso" in cb.alerts[0]

    async def test_the_lock_screen_states_how_many_bets_stay_valid(
        self, session, user_factory
    ):
        """Locking is the one settlement action that moves no money, and the screen
        has to say so — otherwise an admin reads «blocca» and assumes a refund."""
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("lock", event.id))

        await _invoke(ab.cb_admin_lock, cb, session)

        text = cb.message.texts[-1]
        assert "2" in text                      # two bets still pending
        assert "restano valide" in text

    async def test_the_resolve_screen_offers_every_option_with_the_pool(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("resolve", event.id))

        await _invoke(ab.cb_admin_resolve, cb, session)

        assert "400" in cb.message.texts[-1]
        labels = [
            b.text for row in cb.message.markups[-1].inline_keyboard for b in row
        ]
        assert any("Casa" in t for t in labels) and any("Trasferta" in t for t in labels)

    async def test_locking_a_missing_event_is_an_alert(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        cb = _FakeCallback(_admin_bet("confirm_lock", 999999))

        await _invoke(ab.cb_admin_confirm_lock, cb, session)

        assert cb.alerts and "non trovato" in cb.alerts[0]


class TestNavigation:
    async def test_the_event_list_renders_with_and_without_events(
        self, session, user_factory, monkeypatch
    ):
        """`_show_event_list` branches on `isinstance(target, CallbackQuery)` to choose
        between editing the panel in place and posting a new one. A stub cannot pass
        that check, so the class the handler tests against is pointed at the stub —
        which is the branch itself being exercised, not a way around it.
        """
        monkeypatch.setattr(ab, "CallbackQuery", _FakeCallback)

        # No seeding here: the empty case must hold on a database with no rows at all,
        # and `_event_with_bets` below creates the admin itself.
        empty = _FakeCallback(_admin_bet("list"))
        await ab.cb_admin_list(empty, session)
        assert "Nessun evento attivo" in empty.message.texts[-1]

        await _event_with_bets(session, user_factory)
        full = _FakeCallback(_admin_bet("list"))
        await ab.cb_admin_list(full, session)
        assert "Nessun evento attivo" not in full.message.texts[-1]


class TestEntryPoint:
    async def test_in_private_the_panel_is_posted(self, session, user_factory):
        await _event_with_bets(session, user_factory)
        message = _FakeMessage()

        await ab.cmd_gestisci_scommesse(message, session)

        assert "Gestione Scommesse" in message.texts[-1]

    async def test_in_a_group_it_redirects_to_private_instead(self, session, user_factory):
        """The panel lists every open bet and its pool. Rendering that in the group
        would show the players the state of a bet they are still betting on, so the
        group answer is a deep link and nothing else."""
        await user_factory(tg_id=ADMIN_ID)

        class _GroupMessage(_FakeMessage):
            def __init__(self):
                super().__init__()
                self.chat = SimpleNamespace(id=-100123, type="supergroup")
                self.bot.get_me = self._get_me
                self.replies: list[str] = []

            async def _get_me(self):
                return SimpleNamespace(username="testbot")

            async def reply(self, text, reply_markup=None):
                self.replies.append(text)
                self.markups.append(reply_markup)

        message = _GroupMessage()
        await ab.cmd_gestisci_scommesse(message, session)

        assert message.replies and "chat privata" in message.replies[0]
        assert message.texts == [], "the panel itself was rendered in the group"
        url = message.markups[-1].inline_keyboard[0][0].url
        assert url.endswith("?start=manage_bets")

    async def test_the_event_menu_shows_the_pool_and_the_open_bets(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        cb = _FakeCallback(_admin_bet("event", event.id))

        await _invoke(ab.cb_admin_event, cb, session)

        text = cb.message.texts[-1]
        assert "400" in text          # pool
        assert "Casa" in text and "Trasferta" in text

    async def test_menus_for_a_missing_event_alert_instead_of_rendering(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID)
        for handler, data in (
            (ab.cb_admin_event, _admin_bet("event", 999999)),
            (ab.cb_admin_lock, _admin_bet("lock", 999999)),
            (ab.cb_admin_resolve, _admin_bet("resolve", 999999)),
            (ab.cb_admin_cancel, _admin_bet("cancel", 999999)),
        ):
            cb = _FakeCallback(data)
            await _invoke(handler, cb, session)
            assert cb.alerts and "non trovato" in cb.alerts[0], data
            assert cb.message.texts == [], data

    async def test_resolve_screen_refuses_an_already_settled_event(
        self, session, user_factory
    ):
        event = await _event_with_bets(session, user_factory)
        cancelled = _FakeCallback(_admin_bet("confirm_cancel", event.id))
        await _invoke(ab.cb_admin_confirm_cancel, cancelled, session)
        cb = _FakeCallback(_admin_bet("resolve", event.id))

        await _invoke(ab.cb_admin_resolve, cb, session)

        assert cb.alerts and "già chiuso" in cb.alerts[0]

    async def test_close_deletes_the_panel_and_survives_a_failed_delete(self):
        cb = _FakeCallback(_admin_bet("close"))
        await ab.cb_admin_close(cb)
        assert cb.message.deleted

        class _Undeletable(_FakeMessage):
            async def delete(self):
                raise RuntimeError("message to delete not found")

        # Telegram refuses to delete messages older than 48h; the panel must still close.
        await ab.cb_admin_close(_FakeCallback(_admin_bet("close"), _Undeletable()))

    async def test_non_admins_get_a_denial(self):
        cb = _FakeCallback(_admin_bet("event", 1), user_id=999)
        await ab.cb_admin_deny(cb)
        assert cb.alerts and "non autorizzato" in cb.alerts[0]
