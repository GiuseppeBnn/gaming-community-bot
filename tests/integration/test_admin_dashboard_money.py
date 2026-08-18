"""The admin dashboard's money and XP actions — `handlers/admin_dashboard.py`.

`test_admin_dashboard.py` covers the shared warn helper and the dossier renderer.
This file covers the other half, which had no tests at all: the FSM steps where an
admin types a number and coins or XP move. Five actions go through one handler
(`fsm_amount`) and they are **not** interchangeable — credit adds, setbal replaces,
xpgrant adds, xpset replaces — plus two airdrops that touch every user at once.

What is pinned here, in order of how much it would cost to get wrong:

  * the arithmetic of each action, read back from the database by column;
  * the audit row, because an admin moving coins with no trace is the one thing that
    cannot be reconstructed afterwards;
  * the validation floors, which differ per action on purpose: `setbal 0` is a
    legitimate "wipe this balance", `credit 0` is a mistake;
  * the failure paths leaving the FSM state intact, so a rejected amount can be
    retyped instead of dropping the admin back to the start.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from database.models import AdminAction, User, Wallet
from handlers.callbacks import AdminCb
from handlers import admin_dashboard as ad

ADMIN_ID = 1
TARGET_ID = 42


class _FakeBot:
    id = 777

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id, user_id):  # pragma: no cover - group off
        raise RuntimeError("no group configured in tests")


class _BlockedBot(_FakeBot):
    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("Forbidden: bot was blocked by the user")


class _FakeMessage:
    def __init__(self, text: str, bot=None, user_id: int = ADMIN_ID) -> None:
        self.text = text
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.answers: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.answers.append(text)


class _FakeCallback:
    def __init__(self, data: str, user_id: int = ADMIN_ID) -> None:
        self.data = data
        self.message = _FakeMessage("", user_id=user_id)
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


async def _armed(action: str, target: int = TARGET_ID) -> FSMContext:
    """An FSM already carrying what `cb_act` would have put there."""
    state = _state()
    await state.set_state(ad.AdminPanelStates.waiting_amount)
    await state.update_data(action=action, target_tg_id=target)
    return state


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _xp(session, tg_id: int) -> int:
    return (await session.execute(select(User.xp).where(User.tg_id == tg_id))).scalar_one()


async def _audit(session) -> list[AdminAction]:
    return list((await session.execute(select(AdminAction))).scalars().all())


class TestCoinActions:
    async def test_credit_adds_and_leaves_a_trace(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, coins=100)
        message = _FakeMessage("250")

        await ad.fsm_amount(message, await _armed("credit"), session)

        assert await _coins(session, TARGET_ID) == 350
        [row] = await _audit(session)
        assert row.action_type == "credita"
        assert row.amount == 250 and row.target_tg_id == TARGET_ID
        assert message.bot.sent and "250" in message.bot.sent[0][1]

    async def test_debit_subtracts_and_records_a_negative_amount(
        self, session, user_factory
    ):
        """The audit stores `-amount` for a debit so the log can be summed straight
        into a net movement instead of needing the action type to know the sign."""
        await user_factory(tg_id=TARGET_ID, coins=500)
        message = _FakeMessage("200")

        await ad.fsm_amount(message, await _armed("debit"), session)

        assert await _coins(session, TARGET_ID) == 300
        [row] = await _audit(session)
        assert row.action_type == "addebita" and row.amount == -200

    async def test_setbal_replaces_rather_than_adding(self, session, user_factory):
        """The distinction that makes the three coin actions non-interchangeable: this
        one lands on the number typed, whatever the balance was."""
        await user_factory(tg_id=TARGET_ID, coins=9999)
        message = _FakeMessage("50")

        await ad.fsm_amount(message, await _armed("setbal"), session)

        assert await _coins(session, TARGET_ID) == 50
        [row] = await _audit(session)
        assert row.action_type == "setsaldo"
        assert row.detail == "9999 → 50", "the audit must show where the balance came from"

    async def test_setbal_accepts_zero_but_credit_does_not(self, session, user_factory):
        """Different floors on purpose: wiping a balance to 0 is a real operation,
        crediting 0 is a typo. Both arrive through the same handler."""
        await user_factory(tg_id=TARGET_ID, coins=700)

        await ad.fsm_amount(_FakeMessage("0"), await _armed("setbal"), session)
        assert await _coins(session, TARGET_ID) == 0

        rejected = _FakeMessage("0")
        await ad.fsm_amount(rejected, await _armed("credit"), session)
        assert await _coins(session, TARGET_ID) == 0
        assert rejected.answers and "non valido" in rejected.answers[0]

    async def test_debiting_more_than_the_balance_changes_nothing(
        self, session, user_factory
    ):
        await user_factory(tg_id=TARGET_ID, coins=100)
        message = _FakeMessage("500")
        state = await _armed("debit")

        await ad.fsm_amount(message, state, session)

        assert await _coins(session, TARGET_ID) == 100
        assert await _audit(session) == []
        assert message.answers and "Fondi insufficienti" in message.answers[0]
        assert await state.get_state() is not None, (
            "the FSM was cleared, so retyping a smaller amount would go nowhere"
        )

    async def test_an_unknown_target_is_reported_not_crashed(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        message = _FakeMessage("10")

        await ad.fsm_amount(message, await _armed("credit", target=999999), session)

        assert message.answers, "the admin got no feedback at all"
        assert await _audit(session) == []

    async def test_a_blocked_target_still_gets_the_coins(self, session, user_factory):
        """The DM is a courtesy; the credit is the operation. A user who never opened
        the bot in private must not make the transfer fail."""
        await user_factory(tg_id=TARGET_ID, coins=0)
        message = _FakeMessage("75", bot=_BlockedBot())

        await ad.fsm_amount(message, await _armed("credit"), session)

        assert await _coins(session, TARGET_ID) == 75


class TestXpActions:
    async def test_xpgrant_adds(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, xp=100)
        message = _FakeMessage("400")

        await ad.fsm_amount(message, await _armed("xpgrant"), session)

        assert await _xp(session, TARGET_ID) == 500
        [row] = await _audit(session)
        assert row.action_type == "xp_grant" and row.amount == 400
        assert message.bot.sent and "400" in message.bot.sent[0][1]

    async def test_xpset_replaces(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, xp=5000)
        message = _FakeMessage("10")

        await ad.fsm_amount(message, await _armed("xpset"), session)

        assert await _xp(session, TARGET_ID) == 10
        [row] = await _audit(session)
        assert row.action_type == "xp_set" and row.amount == 10

    async def test_a_grant_that_levels_the_user_up_says_so(self, session, user_factory):
        """The DM is the only place the user learns they levelled up from an admin
        grant — there is no other notification for it."""
        await user_factory(tg_id=TARGET_ID, xp=0)
        message = _FakeMessage("100000")

        await ad.fsm_amount(message, await _armed("xpgrant"), session)

        dm = message.bot.sent[0][1]
        assert "livello" in dm
        assert "rango" in dm.lower()

    async def test_xpset_accepts_zero(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, xp=800)
        await ad.fsm_amount(_FakeMessage("0"), await _armed("xpset"), session)
        assert await _xp(session, TARGET_ID) == 0


class TestAmountValidation:
    async def test_a_non_number_is_refused_without_touching_anything(
        self, session, user_factory
    ):
        await user_factory(tg_id=TARGET_ID, coins=100)
        message = _FakeMessage("tanti")

        await ad.fsm_amount(message, await _armed("credit"), session)

        assert await _coins(session, TARGET_ID) == 100
        assert message.answers and "non valido" in message.answers[0]

    async def test_an_absurd_amount_is_refused(self, session, user_factory):
        """The cap is what stops a stray keypress from minting a number that no
        longer fits the economy — and, past int32, not the column either."""
        await user_factory(tg_id=TARGET_ID, coins=100)
        message = _FakeMessage(str(ad._MAX_AMOUNT + 1))

        await ad.fsm_amount(message, await _armed("credit"), session)

        assert await _coins(session, TARGET_ID) == 100
        assert message.answers and "non valido" in message.answers[0]

    async def test_a_negative_amount_is_refused(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, coins=100)
        message = _FakeMessage("-50")

        await ad.fsm_amount(message, await _armed("credit"), session)

        assert await _coins(session, TARGET_ID) == 100


class TestAirdrops:
    async def test_a_coin_airdrop_reaches_everyone(self, session, user_factory):
        for tg_id in (10, 11, 12):
            await user_factory(tg_id=tg_id, coins=100)
        state = _state()
        message = _FakeMessage("50")

        await ad.fsm_airdrop(message, state, session)

        assert [await _coins(session, i) for i in (10, 11, 12)] == [150, 150, 150]
        [row] = await _audit(session)
        assert row.action_type == "airdrop" and row.amount == 50
        assert "3" in row.detail, "the audit must record how many users were paid"
        assert await state.get_state() is None, "the FSM stayed armed after a success"

    async def test_an_xp_airdrop_reaches_everyone(self, session, user_factory):
        for tg_id in (10, 11):
            await user_factory(tg_id=tg_id, xp=7)
        message = _FakeMessage("93")

        await ad.fsm_xp_airdrop(message, _state(), session)

        assert [await _xp(session, i) for i in (10, 11)] == [100, 100]
        [row] = await _audit(session)
        assert row.action_type == "xp_airdrop" and row.amount == 93

    async def test_an_invalid_airdrop_pays_nobody(self, session, user_factory):
        await user_factory(tg_id=10, coins=100)

        for text in ("0", "-5", "molti", str(ad._MAX_AMOUNT + 1)):
            message = _FakeMessage(text)
            await ad.fsm_airdrop(message, _state(), session)
            assert message.answers and "non valido" in message.answers[0], text

        assert await _coins(session, 10) == 100
        assert await _audit(session) == []

    async def test_an_invalid_xp_airdrop_pays_nobody(self, session, user_factory):
        await user_factory(tg_id=10, xp=5)
        message = _FakeMessage("0")

        await ad.fsm_xp_airdrop(message, _state(), session)

        assert await _xp(session, 10) == 5
        assert message.answers and "non valido" in message.answers[0]


class TestMassReward:
    """The «Manda premi» flow: pick XP/CoInn → amount → an @username list. The
    money must reach exactly the matched users, the unmatched ones must be
    reported, and an all-miss list must let the admin retype instead of dropping
    them out of the flow."""

    async def test_entry_asks_what_to_send(self):
        callback_data = AdminCb(action="massreward")
        cb = _FakeCallback(callback_data.pack())
        await ad.cb_massreward(cb, callback_data, _state())
        assert "Manda premi" in cb.message.answers[-1]

    async def test_choosing_a_type_arms_the_amount_step(self):
        state = _state()
        callback_data = AdminCb(action="massreward", key="coins")
        await ad.cb_massreward(_FakeCallback(callback_data.pack()), callback_data, state)
        assert await state.get_state() == ad.AdminPanelStates.waiting_mass_amount
        assert (await state.get_data())["mass_kind"] == "coins"

    async def test_the_amount_validates_then_asks_for_recipients(self):
        state = _state()
        await state.set_state(ad.AdminPanelStates.waiting_mass_amount)
        await state.update_data(mass_kind="coins")

        bad = _FakeMessage("tanti")
        await ad.fsm_mass_amount(bad, state)
        assert await state.get_state() == ad.AdminPanelStates.waiting_mass_amount
        assert bad.answers and "non valido" in bad.answers[0]

        good = _FakeMessage("100")
        await ad.fsm_mass_amount(good, state)
        assert await state.get_state() == ad.AdminPanelStates.waiting_mass_recipients
        assert (await state.get_data())["mass_amount"] == 100

    async def _armed_recipients(self, kind: str, amount: int) -> FSMContext:
        state = _state()
        await state.set_state(ad.AdminPanelStates.waiting_mass_recipients)
        await state.update_data(mass_kind=kind, mass_amount=amount)
        return state

    async def test_coins_reach_matched_users_and_the_missing_are_reported(
        self, session, user_factory
    ):
        await user_factory(tg_id=10, username="alice", coins=0)
        await user_factory(tg_id=11, username="bob", coins=0)
        message = _FakeMessage("@alice\nbob\n@ghost")

        await ad.fsm_mass_recipients(message, await self._armed_recipients("coins", 100), session)

        assert await _coins(session, 10) == 100 and await _coins(session, 11) == 100
        [row] = await _audit(session)
        assert row.action_type == "mass_credit" and row.amount == 100
        assert "2" in (row.detail or "")
        assert any("ghost" in a for a in message.answers), "unmatched names must be reported"
        # Both matched users were DM'd.
        assert {c for c, _t in message.bot.sent} == {10, 11}

    async def test_xp_reaches_matched_users(self, session, user_factory):
        await user_factory(tg_id=10, username="alice", xp=0)
        message = _FakeMessage("@alice")

        await ad.fsm_mass_recipients(message, await self._armed_recipients("xp", 50), session)

        assert await _xp(session, 10) == 50
        [row] = await _audit(session)
        assert row.action_type == "mass_xp" and row.amount == 50

    async def test_no_matched_username_keeps_the_state_to_retry(self, session, user_factory):
        await user_factory(tg_id=10, username="alice", coins=0)
        state = await self._armed_recipients("coins", 100)
        message = _FakeMessage("@ghost\n@nobody")

        await ad.fsm_mass_recipients(message, state, session)

        assert await _audit(session) == [] and await _coins(session, 10) == 0
        assert await state.get_state() == ad.AdminPanelStates.waiting_mass_recipients


class TestActionRouting:
    async def test_each_money_action_arms_the_amount_step_for_its_target(self):
        """`cb_act` is the only place the action name and the target id get into the
        FSM. If it armed the wrong state, the admin's next message would be read by a
        different handler — or by none."""
        for action in ("credit", "debit", "setbal", "xpgrant", "xpset"):
            state = _state()
            callback_data = AdminCb(action="act", key=action, item_id=TARGET_ID)
            cb = _FakeCallback(callback_data.pack())

            await ad.cb_act(cb, callback_data, state)

            data = await state.get_data()
            assert data == {"action": action, "target_tg_id": TARGET_ID}, action
            assert await state.get_state() == ad.AdminPanelStates.waiting_amount, action

    async def test_mute_and_warn_arm_their_own_steps(self, monkeypatch):
        """Not the amount step: a duration and a reason are read by different handlers,
        so arming the wrong one would silently swallow the admin's next message."""
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        for action, expected in (
            ("mute", ad.AdminPanelStates.waiting_duration),
            ("warn", ad.AdminPanelStates.waiting_reason),
        ):
            state = _state()
            callback_data = AdminCb(action="act", key=action, item_id=TARGET_ID)
            await ad.cb_act(_FakeCallback(callback_data.pack()), callback_data, state)
            assert await state.get_state() == expected, action

    async def test_a_warn_with_no_reason_still_counts(
        self, session, user_factory, monkeypatch
    ):
        """The «Senza motivo» button skips the text step entirely and lands in `cb_do`.
        It has to produce the same warn — and the same escalation — as a typed one."""
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=TARGET_ID)
        callback_data = AdminCb(action="do", key="warn", item_id=TARGET_ID)
        cb = _FakeCallback(callback_data.pack())
        cb.message.bot = _ModBot()
        cb.bot = cb.message.bot

        await ad.cb_do(cb, callback_data, _state(), session)

        assert await ad.admin_service.active_warning_count(session, TARGET_ID) == 1
        [row] = [r for r in await _audit(session) if r.action_type == "warn"]
        assert row.amount == 1, "the audit records the resulting warn count"
        assert row.detail is None, "no reason was given, so none must be invented"

    async def test_moderating_yourself_is_refused_before_anything_is_armed(
        self, monkeypatch
    ):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        state = _state()
        callback_data = AdminCb(action="act", key="warn", item_id=ADMIN_ID)
        cb = _FakeCallback(callback_data.pack())  # target == admin

        await ad.cb_act(cb, callback_data, state)

        assert cb.alerts and "te stesso" in cb.alerts[0]
        assert await state.get_state() is None

    async def test_moderating_the_bot_is_refused(self, monkeypatch):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        state = _state()
        callback_data = AdminCb(action="act", key="mute", item_id=_FakeBot.id)
        cb = _FakeCallback(callback_data.pack())

        await ad.cb_act(cb, callback_data, state)

        assert cb.alerts and "me stesso" in cb.alerts[0]

    async def test_without_a_group_moderation_is_refused(self, monkeypatch):
        """Every moderation action needs a chat to act on; without `GROUP_ID` the
        call would go to Telegram with a zero chat id."""
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: 0)
        callback_data = AdminCb(action="act", key="warn", item_id=TARGET_ID)
        cb = _FakeCallback(callback_data.pack())

        await ad.cb_act(cb, callback_data, _state())

        assert cb.alerts and "GROUP_ID" in cb.alerts[0]

    async def test_non_admins_are_denied(self):
        cb = _FakeCallback(AdminCb(action="home").pack(), user_id=999)
        await ad.cb_deny(cb)
        assert cb.alerts and "non autorizzato" in cb.alerts[0]


class TestNavigation:
    """The read-only screens. Individually dull, collectively the thing that turns a
    renderer crash into «the admin panel is broken» — every one of these builds text
    and a keyboard out of live data, and none of them was ever executed by a test.
    """

    async def test_every_read_only_screen_renders(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, coins=250, xp=900)

        for handler, callback_data in (
            (ad.cb_stats, AdminCb(action="stats")),
            (ad.cb_lead, AdminCb(action="lead")),
            (ad.cb_audit, AdminCb(action="audit")),
        ):
            cb = _FakeCallback(callback_data.pack())
            await handler(cb, session)
            assert cb.message.answers, callback_data.action

        help_cb = _FakeCallback(AdminCb(action="help").pack())
        await ad.cb_help(help_cb)
        assert help_cb.message.answers

    async def test_the_leaderboard_switches_board_and_ignores_junk(
        self, session, user_factory
    ):
        await user_factory(tg_id=TARGET_ID, coins=250, xp=900)

        for board in ("coins", "xp", "trofei"):
            callback_data = AdminCb(action="lead_board", key=board)
            cb = _FakeCallback(callback_data.pack())
            await ad.cb_lead_board(cb, callback_data, session)
            assert cb.message.answers, board

        junk_data = AdminCb(action="lead_board", key="oroscopo")
        junk = _FakeCallback(junk_data.pack())
        await ad.cb_lead_board(junk, junk_data, session)
        assert junk.message.answers == [], "an unknown board still rendered something"

    async def test_home_renders_from_a_command_and_from_a_button(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(ad, "CallbackQuery", _FakeCallback)
        await user_factory(tg_id=TARGET_ID, coins=10)

        message = _FakeMessage("/admin")
        await ad.cmd_admin(message, session)
        assert message.answers and "Dashboard Admin" in message.answers[0]

        cb = _FakeCallback(AdminCb(action="home").pack())
        await ad.cb_home(cb, _state(), session)
        assert cb.message.answers and "Dashboard Admin" in cb.message.answers[-1]

    async def test_home_falls_back_to_a_new_message_when_the_edit_fails(
        self, session, user_factory, monkeypatch
    ):
        """Telegram rejects an edit whose result is identical to the current text.
        Tapping «Home» while already on Home must not leave the admin with a dead
        button, so the failure is caught and a fresh message is posted instead."""
        monkeypatch.setattr(ad, "CallbackQuery", _FakeCallback)
        await user_factory(tg_id=TARGET_ID)

        cb = _FakeCallback(AdminCb(action="home").pack())

        async def refuse_edit(text, reply_markup=None, **kw):
            raise RuntimeError("Bad Request: message is not modified")

        cb.message.edit_text = refuse_edit

        await ad.cb_home(cb, _state(), session)

        assert cb.message.answers, "the fallback message was never sent"

    async def test_admin_in_a_group_redirects_to_private(self, session, user_factory):
        """The dashboard shows balances, audit log and moderation buttons. Rendering
        it in the group would publish all of it."""
        await user_factory(tg_id=ADMIN_ID)

        class _GroupMessage(_FakeMessage):
            def __init__(self):
                super().__init__("/admin")
                self.chat = SimpleNamespace(id=-100123, type="supergroup")
                self.replies: list[str] = []
                self.markups: list[object] = []
                self.bot.get_me = self._get_me

            async def _get_me(self):
                return SimpleNamespace(username="testbot")

            async def reply(self, text, reply_markup=None):
                self.replies.append(text)
                self.markups.append(reply_markup)

        message = _GroupMessage()
        await ad.cmd_admin(message, session)

        assert message.replies and "privata" in message.replies[0]
        assert message.answers == [], "the dashboard was rendered in the group"
        assert message.markups[-1].inline_keyboard[0][0].url.endswith("?start=admin")

    async def test_the_menus_that_only_arm_a_state(self, session, user_factory):
        for handler, callback_data, expected in (
            (ad.cb_econ, AdminCb(action="econ"), None),
            (ad.cb_airdrop, AdminCb(action="airdrop"), ad.AdminPanelStates.waiting_airdrop),
            (ad.cb_xpairdrop, AdminCb(action="xpairdrop"), ad.AdminPanelStates.waiting_xp_airdrop),
            (ad.cb_search, AdminCb(action="search"), ad.AdminPanelStates.waiting_search),
        ):
            state = _state()
            cb = _FakeCallback(callback_data.pack())
            await handler(cb, state)
            assert cb.message.answers, handler.__name__
            assert await state.get_state() == expected, handler.__name__

    async def test_close_deletes_the_panel_and_survives_a_failed_delete(self):
        deleted: list[bool] = []

        async def record():
            deleted.append(True)

        cb = _FakeCallback(AdminCb(action="close").pack())
        cb.message.delete = record
        await ad.cb_close(cb, _state())
        assert deleted

        async def refuse():
            raise RuntimeError("message to delete not found")

        # Telegram refuses to delete messages older than 48h; the panel must still close.
        stubborn = _FakeCallback(AdminCb(action="close").pack())
        stubborn.message.delete = refuse
        await ad.cb_close(stubborn, _state())  # must not raise


class TestUserPickerAndSearch:
    async def test_the_picker_paginates_and_reports_an_empty_database(
        self, session, user_factory
    ):
        empty_data = AdminCb(action="users", item_id=0)
        empty = _FakeCallback(empty_data.pack())
        await ad.cb_users(empty, empty_data, _state(), session)
        assert "Nessun utente" in empty.message.answers[-1]

        for tg_id in range(100, 100 + ad.PAGE_SIZE + 3):
            await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=1)

        first_data = AdminCb(action="users", item_id=0)
        first = _FakeCallback(first_data.pack())
        await ad.cb_users(first, first_data, _state(), session)
        assert "pagina 1" in first.message.answers[-1]

        second_data = AdminCb(action="users", item_id=1)
        second = _FakeCallback(second_data.pack())
        await ad.cb_users(second, second_data, _state(), session)
        assert "pagina 2" in second.message.answers[-1]

    async def test_a_negative_page_is_clamped_instead_of_querying_backwards(
        self, session, user_factory
    ):
        """`page * PAGE_SIZE` becomes a negative OFFSET otherwise, which SQL rejects."""
        await user_factory(tg_id=TARGET_ID, coins=1)
        callback_data = AdminCb(action="users", item_id=-3)
        cb = _FakeCallback(callback_data.pack())

        await ad.cb_users(cb, callback_data, _state(), session)

        assert "pagina 1" in cb.message.answers[-1]

    async def test_search_finds_by_username_and_says_so_when_it_does_not(
        self, session, user_factory
    ):
        await user_factory(tg_id=TARGET_ID, username="pippo", coins=5)

        found = _FakeMessage("pippo")
        await ad.fsm_search(found, _state(), session)
        assert "Risultati" in found.answers[0] and "1" in found.answers[0]

        missing = _FakeMessage("qwertyuiop")
        await ad.fsm_search(missing, _state(), session)
        assert "Nessun utente trovato" in missing.answers[0]

    async def test_a_one_character_search_is_refused(self, session, user_factory):
        """Without the floor the query matches most of the table and the reply is a
        keyboard of everyone."""
        await user_factory(tg_id=TARGET_ID, username="pippo")
        message = _FakeMessage("p")

        await ad.fsm_search(message, _state(), session)

        assert "almeno 2 caratteri" in message.answers[0]

    async def test_opening_a_user_shows_the_dossier(self, session, user_factory):
        await user_factory(tg_id=TARGET_ID, username="pippo", coins=333)
        callback_data = AdminCb(action="user", item_id=TARGET_ID)
        cb = _FakeCallback(callback_data.pack())

        await ad.cb_user(cb, callback_data, _state(), session)

        assert "333" in cb.message.answers[-1]

    async def test_opening_an_unknown_user_says_so(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID)
        callback_data = AdminCb(action="user", item_id=999_999)
        cb = _FakeCallback(callback_data.pack())

        await ad.cb_user(cb, callback_data, _state(), session)

        assert "non trovato" in cb.message.answers[-1]


class _ModBot(_FakeBot):
    """A bot whose group calls succeed, recording what was asked of Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.banned: list[int] = []
        self.unbanned: list[int] = []
        self.restricted: list[tuple[int, int | None]] = []

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append(user_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
        self.restricted.append((user_id, until_date))

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


class _RefusingBot(_ModBot):
    """Telegram refuses the group action — typically the target is an admin, or the
    bot has no rights."""

    async def ban_chat_member(self, chat_id, user_id, **kw):
        raise RuntimeError("Bad Request: user is an administrator of the chat")

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
        raise RuntimeError("Bad Request: not enough rights")


class TestModerationActions:
    """`cb_do` executes the confirmed moderation actions. These leave no money trail,
    but they are the actions a user notices most, and each one has to end up in the
    audit log with the right type — that log is what an admin reads when a member
    asks «why was I banned»."""

    def _cb(self, action: str, bot=None, target: int = TARGET_ID) -> tuple[_FakeCallback, AdminCb]:
        callback_data = AdminCb(action="do", key=action, item_id=target)
        cb = _FakeCallback(callback_data.pack())
        if bot is not None:
            cb.message.bot = bot
            cb.bot = bot
        return cb, callback_data

    async def _group(self, monkeypatch):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)

    async def test_ban_marks_the_user_in_the_database_too(
        self, session, user_factory, monkeypatch
    ):
        """The bot-level ban is the part that matters here: removing someone from the
        group does not stop them using the bot in private, so the flag has to land
        even when Telegram accepts the removal."""
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()
        cb, callback_data = self._cb("ban", bot)

        await ad.cb_do(cb, callback_data, _state(), session)

        assert bot.banned == [TARGET_ID]
        banned = await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID))
        assert banned is True
        assert [r.action_type for r in await _audit(session)] == ["ban"]

    async def test_a_refused_group_removal_still_bans_from_the_bot(
        self, session, user_factory, monkeypatch
    ):
        """Telegram refuses to remove group admins. The intent was still expressed, so
        the bot-level ban lands anyway and the admin gets a warning toast rather than
        a silent no-op."""
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        cb, callback_data = self._cb("ban", _RefusingBot())

        await ad.cb_do(cb, callback_data, _state(), session)

        banned = await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID))
        assert banned is True
        assert cb.alerts, "the admin was not warned that the group removal failed"

    async def test_kick_removes_and_lets_them_come_back(
        self, session, user_factory, monkeypatch
    ):
        """A kick is a ban immediately lifted — without the unban it would be a ban
        under a friendlier name."""
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()

        cb, callback_data = self._cb("kick", bot)
        await ad.cb_do(cb, callback_data, _state(), session)

        assert bot.banned == [TARGET_ID] and bot.unbanned == [TARGET_ID]
        assert [r.action_type for r in await _audit(session)] == ["kick"]

    async def test_sban_clears_the_flag(self, session, user_factory, monkeypatch):
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        cb, callback_data = self._cb("ban", _ModBot())
        await ad.cb_do(cb, callback_data, _state(), session)

        cb, callback_data = self._cb("sban", _ModBot())
        await ad.cb_do(cb, callback_data, _state(), session)

        banned = await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID))
        assert banned is False
        assert [r.action_type for r in await _audit(session)] == ["ban", "sban"]

    async def test_unmute_lifts_the_restriction(self, session, user_factory, monkeypatch):
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()

        cb, callback_data = self._cb("unmute", bot)
        await ad.cb_do(cb, callback_data, _state(), session)

        assert bot.restricted and bot.restricted[0][0] == TARGET_ID
        assert [r.action_type for r in await _audit(session)] == ["unmute"]

    async def test_unwarn_removes_one_and_reports_what_is_left(
        self, session, user_factory, monkeypatch
    ):
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()
        for _ in range(2):
            await ad.apply_warning(bot, session, ADMIN_ID, TARGET_ID, -100123, None)
        await session.commit()

        cb, callback_data = self._cb("unwarn", bot)
        await ad.cb_do(cb, callback_data, _state(), session)

        remaining = await ad.admin_service.active_warning_count(session, TARGET_ID)
        assert remaining == 1
        [row] = [r for r in await _audit(session) if r.action_type == "unwarn"]
        assert row.amount == 1, "the audit must record how many warns are left"

    async def test_unwarn_on_a_clean_user_says_so(
        self, session, user_factory, monkeypatch
    ):
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        cb, callback_data = self._cb("unwarn", _ModBot())

        await ad.cb_do(cb, callback_data, _state(), session)

        assert cb.alerts and "Nessun warn" in cb.alerts[0]

    async def test_an_unknown_action_does_nothing(self, session, user_factory, monkeypatch):
        await self._group(monkeypatch)
        await user_factory(tg_id=TARGET_ID)
        cb, callback_data = self._cb("teleport", _ModBot())

        await ad.cb_do(cb, callback_data, _state(), session)

        assert await _audit(session) == []

    async def test_moderating_yourself_is_refused(self, session, user_factory, monkeypatch):
        await self._group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID)
        cb, callback_data = self._cb("ban", _ModBot(), target=ADMIN_ID)

        await ad.cb_do(cb, callback_data, _state(), session)

        assert cb.alerts and "te stesso" in cb.alerts[0]
        assert await _audit(session) == []

    async def test_the_confirmation_screen_names_the_action(self, monkeypatch):
        await self._group(monkeypatch)
        callback_data = AdminCb(action="ask", key="ban", item_id=TARGET_ID)
        cb = _FakeCallback(callback_data.pack())
        cb.message.bot = _ModBot()
        cb.bot = cb.message.bot

        await ad.cb_ask(cb, callback_data)

        assert "BAN" in cb.message.answers[-1] and str(TARGET_ID) in cb.message.answers[-1]


class TestMuteAndWarnInput:
    async def test_a_valid_duration_mutes_for_that_long(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()
        message = _FakeMessage("10m", bot=bot)
        state = _state()
        await state.update_data(target_tg_id=TARGET_ID)

        await ad.fsm_duration(message, state, session)

        assert bot.restricted and bot.restricted[0][0] == TARGET_ID
        [row] = await _audit(session)
        assert row.action_type == "mute" and row.amount == 600

    async def test_a_bad_duration_is_refused_and_lets_you_retype(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=TARGET_ID)
        bot = _ModBot()
        message = _FakeMessage("presto", bot=bot)
        state = _state()
        await state.update_data(target_tg_id=TARGET_ID)

        await ad.fsm_duration(message, state, session)

        assert bot.restricted == []
        assert message.answers and "Durata non valida" in message.answers[0]
        assert (await state.get_data())["target_tg_id"] == TARGET_ID

    async def test_a_refused_mute_reports_the_reason_and_logs_nothing(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=TARGET_ID)
        message = _FakeMessage("1h", bot=_RefusingBot())
        state = _state()
        await state.update_data(target_tg_id=TARGET_ID)

        await ad.fsm_duration(message, state, session)

        assert await _audit(session) == [], "a mute that never happened was logged"
        assert message.answers

    async def test_a_warn_with_a_reason_records_it(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=TARGET_ID)
        message = _FakeMessage("spam nel gruppo", bot=_ModBot())
        state = _state()
        await state.update_data(target_tg_id=TARGET_ID)

        await ad.fsm_reason(message, state, session)

        [row] = [r for r in await _audit(session) if r.action_type == "warn"]
        assert row.target_tg_id == TARGET_ID
        assert row.detail == "spam nel gruppo", "the reason must reach the audit log"
        assert await state.get_state() is None

    async def test_a_warn_on_yourself_is_refused(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(ad.group_registry, "get_group_id", lambda: -100123)
        await user_factory(tg_id=ADMIN_ID)
        message = _FakeMessage("motivo", bot=_ModBot())
        state = _state()
        await state.update_data(target_tg_id=ADMIN_ID)

        await ad.fsm_reason(message, state, session)

        assert await _audit(session) == []
        assert message.answers and "te stesso" in message.answers[0]


# ---------------------------------------------------------------------------
# The moderation guard, and the screens that must survive a stale message
# ---------------------------------------------------------------------------
#
# `_mod_guard` is checked at three separate points on purpose — asking, doing, and
# typing a duration — because between them an admin can change their mind, tap an
# old keyboard, or aim at themselves. Each point is a place where a moderation
# action would otherwise be carried out on the wrong person.

import pytest

from services import group_registry

GROUP_ID = -100_777


@pytest.fixture
def in_group():
    group_registry.set_runtime_group_id(GROUP_ID)
    yield GROUP_ID
    group_registry.set_runtime_group_id(None)


@pytest.fixture
def as_callback(monkeypatch):
    """Make `isinstance(target, CallbackQuery)` true for the stubs.

    `show_dashboard_home` and `_show_event_list` branch on the type to decide
    whether they are editing a panel or answering a command. A pydantic
    CallbackQuery cannot be built with a controllable `message`, so the module's
    own name is pointed at the stub class: the branch under test is the real one,
    it is only the type check that is satisfied differently.
    """
    from handlers import admin_betting as ab

    monkeypatch.setattr(ad, "CallbackQuery", _FakeCallback)
    monkeypatch.setattr(ab, "CallbackQuery", _FakeCallback)


@pytest.fixture
def no_group():
    group_registry.set_runtime_group_id(0)
    yield 0
    group_registry.set_runtime_group_id(None)


class _StaleMessage(_FakeMessage):
    """A panel Telegram will not re-edit (too old, or unchanged)."""

    async def edit_text(self, text, reply_markup=None, **kw):
        raise RuntimeError("message is not modified")


class _StaleCallback(_FakeCallback):
    def __init__(self, data: str, user_id: int = ADMIN_ID) -> None:
        super().__init__(data, user_id)
        self.message = _StaleMessage("", user_id=user_id)
        self.bot = self.message.bot


class TestModerationGuard:
    @pytest.mark.parametrize("target,expected", [
        (ADMIN_ID, "te stesso"),
        (_FakeBot.id, "me stesso"),
    ])
    async def test_asking_to_moderate_yourself_or_the_bot_is_refused(
        self, session, in_group, target, expected
    ):
        """Banning the bot removes it from the group; banning yourself locks the
        admin out of their own panel. Both are one tap away in the dossier."""
        callback_data = AdminCb(action="ask", key="ban", item_id=target)
        callback = _FakeCallback(callback_data.pack())

        await ad.cb_ask(callback, callback_data)

        assert callback.alerts and expected in callback.alerts[0]
        assert callback.message.answers == [], "no confirmation screen may open"

    async def test_without_a_group_there_is_nothing_to_moderate(self, session, no_group):
        callback_data = AdminCb(action="ask", key="ban", item_id=TARGET_ID)
        callback = _FakeCallback(callback_data.pack())

        await ad.cb_ask(callback, callback_data)

        assert callback.alerts and "GROUP_ID" in callback.alerts[0]

    async def test_the_confirmation_opens_for_a_legitimate_target(self, session, in_group):
        callback_data = AdminCb(action="ask", key="ban", item_id=TARGET_ID)
        callback = _FakeCallback(callback_data.pack())

        await ad.cb_ask(callback, callback_data)

        assert callback.message.answers and "BAN" in callback.message.answers[0]

    async def test_a_warn_without_a_reason_is_guarded_too(self, session, in_group):
        """The «Senza motivo» button skips the reason step, so it needs its own
        check — it is a second entry point into the same action."""
        callback_data = AdminCb(action="do", key="warn", item_id=ADMIN_ID)
        callback = _FakeCallback(callback_data.pack())

        await ad.cb_do(callback, callback_data, _state(), session)

        assert callback.alerts and "te stesso" in callback.alerts[0]
        assert await _audit(session) == []

    async def test_typing_a_duration_for_an_invalid_target_ends_the_flow(
        self, session, user_factory, in_group
    ):
        """The target is decided when the flow starts; by the time the duration is
        typed the admin may be aiming at themselves (or the group id may be gone)."""
        await user_factory(tg_id=ADMIN_ID, coins=0)
        state = _state()
        await state.set_state(ad.AdminPanelStates.waiting_duration)
        await state.update_data(target_tg_id=ADMIN_ID)
        message = _FakeMessage("10m")

        await ad.fsm_duration(message, state, session)

        assert any("te stesso" in a for a in message.answers)
        assert await state.get_state() is None


class TestOptionalCallbackStateGuards:
    @pytest.mark.parametrize(
        ("handler", "callback_data"),
        [
            (ad.cb_users, AdminCb(action="users")),
            (ad.cb_user, AdminCb(action="user")),
            (ad.cb_do, AdminCb(action="do", key="ban")),
            (ad.cb_do, AdminCb(action="do", item_id=TARGET_ID)),
        ],
        ids=("users-missing-page", "user-missing-id", "do-missing-id", "do-missing-key"),
    )
    async def test_incomplete_payload_preserves_the_active_flow(
        self, handler, callback_data, session
    ):
        state = _state()
        await state.set_state(ad.AdminPanelStates.waiting_search)
        await state.update_data(marker="keep")
        callback = _FakeCallback(callback_data.pack())

        await handler(callback, callback_data, state, session)

        assert await state.get_state() == ad.AdminPanelStates.waiting_search
        assert await state.get_data() == {"marker": "keep"}

    @pytest.mark.parametrize(
        ("handler", "callback_data"),
        [
            (ad.cb_users, AdminCb(action="users", item_id=0)),
            (ad.cb_user, AdminCb(action="user", item_id=999_999)),
            (ad.cb_do, AdminCb(action="do", key="ban", item_id=ADMIN_ID)),
        ],
        ids=("users", "user", "do"),
    )
    async def test_complete_payload_still_clears_the_previous_flow(
        self, handler, callback_data, session, in_group
    ):
        state = _state()
        await state.set_state(ad.AdminPanelStates.waiting_search)
        await state.update_data(marker="discard")
        callback = _FakeCallback(callback_data.pack())

        await handler(callback, callback_data, state, session)

        assert await state.get_state() is None
        assert await state.get_data() == {}


class TestStaleScreens:
    async def test_the_dashboard_home_falls_back_to_a_new_message(
        self, session, as_callback
    ):
        """Every «⬅️ Dashboard» button lands here; a failed edit must not leave the
        admin looking at the previous screen."""
        callback = _StaleCallback(AdminCb(action="home").pack())

        await ad.show_dashboard_home(callback, session, edit=True)

        assert callback.message.answers and "Dashboard Admin" in callback.message.answers[-1]

    async def test_the_home_can_also_send_a_fresh_screen_on_purpose(
        self, session, as_callback
    ):
        callback = _FakeCallback(AdminCb(action="home").pack())

        await ad.show_dashboard_home(callback, session, edit=False)

        assert callback.message.answers and "Dashboard Admin" in callback.message.answers[-1]

    async def test_re_tapping_the_current_leaderboard_tab_is_harmless(
        self, session, user_factory
    ):
        """Telegram rejects an edit that changes nothing; that must not surface as
        a dead button."""
        await user_factory(tg_id=TARGET_ID, coins=100)
        callback_data = AdminCb(action="lead_board", key="coins")
        callback = _StaleCallback(callback_data.pack())

        await ad.cb_lead_board(callback, callback_data, session)

        assert callback.answers == [(None, False)]

    async def test_the_user_detail_falls_back_to_a_new_message(
        self, session, user_factory
    ):
        await user_factory(tg_id=TARGET_ID, coins=100)
        callback = _StaleCallback(AdminCb(action="user", item_id=TARGET_ID).pack())

        await ad._show_detail_cb(callback, session, TARGET_ID)

        assert callback.message.answers, "the dossier must appear either way"

    async def test_the_detail_of_an_unknown_user_says_so(self, session):
        """Reachable from an audit line whose user has since been deleted."""
        message = _FakeMessage("")

        await ad._show_detail_msg(message, session, 999_999)

        assert any("non trovato" in a for a in message.answers)


class TestBetsShortcut:
    async def test_the_bets_button_opens_the_betting_panel(
        self, session, user_factory, as_callback
    ):
        """One tap from the dashboard into the panel that settles bets; the button
        exists so an admin never has to remember the command."""
        await user_factory(tg_id=ADMIN_ID, coins=0)
        callback = _FakeCallback(AdminCb(action="bets").pack())

        await ad.cb_bets(callback, session)

        assert callback.message.answers
