"""The user-facing money commands — `handlers/economy.py`.

`/trasferisci` is the only place in the bot where one player moves coins to another,
and it parses its target and its amount out of free text typed into a group chat.
`/daily` is the most-used command there is. Both were at 23% covered.

The service layer is tested elsewhere and thoroughly; nothing here re-tests
`transfer` or `claim_daily`. What is tested is the part only this layer does:

  * turning `"/trasferisci @mario 100"` into a target and an amount, and refusing
    every shape that is not that — because the alternative to refusing is moving the
    wrong amount to the wrong person;
  * choosing what is said **in public**: `/daily` in a group must acknowledge the
    claim without publishing the streak, the XP and the rank to everyone;
  * not letting a courtesy DM or a trophy announcement break an operation that has
    already been committed.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from database.models import Badge, LedgerEntry, TransactionType, User, Wallet
from handlers import economy
from utils import cooldown
from handlers._trophy_announce import group_registry as economy_group_registry

SENDER = 1
TARGET = 2


async def _some_badge(session) -> list[Badge]:
    """Any real Badge row — `announce_trophies` reads `icon_emoji`/`name` off it, so a
    stand-in object would exercise a different code path than production."""
    return list((await session.execute(select(Badge).limit(1))).scalars().all())


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _MuteBot(_FakeBot):
    """The user never opened the bot in private."""

    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("Forbidden: bot can't initiate conversation with a user")


class _FakeMessage:
    def __init__(self, text: str, *, user_id: int = SENDER, chat_type: str = "private",
                 username: str | None = None, bot=None) -> None:
        self.text = text
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(
            id=user_id, username=username, full_name=f"User {user_id}"
        )
        self.chat = SimpleNamespace(
            id=user_id if chat_type == "private" else -100123, type=chat_type
        )
        self.answers: list[str] = []
        self.replies: list[str] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=len(self.answers))

    async def reply(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies))

    @property
    def said(self) -> str:
        return "\n".join(self.answers + self.replies)


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _ledger(session) -> list[LedgerEntry]:
    return list((await session.execute(select(LedgerEntry))).scalars().all())


class TestTransfer:
    async def test_a_transfer_moves_exactly_the_amount_typed(
        self, session, user_factory
    ):
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/trasferisci @mario 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, SENDER) == 400
        assert await _coins(session, TARGET) == 100
        assert "100" in message.said

    async def test_the_target_can_be_given_as_a_numeric_id(self, session, user_factory):
        """Users without a Telegram username can only be paid by id, so both forms
        have to resolve — and `isdigit()` is what tells them apart."""
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username=None, coins=0)
        message = _FakeMessage(f"/trasferisci {TARGET} 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, TARGET) == 100

    async def test_the_at_sign_is_optional(self, session, user_factory):
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/trasferisci mario 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, TARGET) == 100

    async def test_every_malformed_command_moves_nothing(self, session, user_factory):
        """The parser is the whole risk surface of this command. Each of these used to
        be one `int()` away from moving a number nobody typed."""
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)

        for text in (
            "/trasferisci",                    # no arguments at all
            "/trasferisci @mario",             # no amount
            "/trasferisci @mario tanti",       # amount is not a number
            "/trasferisci @mario 12.5",        # not an integer either
            "/trasferisci @mario 0",           # zero
            "/trasferisci @mario -100",        # negative: would reverse the direction
            "/trasferisci @nessuno 100",       # unknown target
        ):
            message = _FakeMessage(text, username="mittente")
            await economy.cmd_trasferisci(message, session)
            assert message.said, f"no feedback for: {text}"

        assert await _coins(session, SENDER) == 500
        assert await _coins(session, TARGET) == 0
        assert await _ledger(session) == []

    async def test_transferring_to_yourself_is_refused(self, session, user_factory):
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        message = _FakeMessage("/trasferisci @mittente 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, SENDER) == 500
        assert "te stesso" in message.said

    async def test_transferring_more_than_you_have_is_refused(
        self, session, user_factory
    ):
        await user_factory(tg_id=SENDER, username="mittente", coins=50)
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/trasferisci @mario 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, SENDER) == 50
        assert await _coins(session, TARGET) == 0
        assert "insufficiente" in message.said

    async def test_a_target_row_without_a_wallet_is_reported(self, session, user_factory):
        """A `User` can exist without a `Wallet` — an old row, or a half-finished
        registration. Resolving the name then succeeds and the debit does not, so the
        sender must be told rather than seeing a traceback."""
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        session.add(User(tg_id=TARGET, username="mario", full_name="Mario"))
        await session.commit()
        message = _FakeMessage("/trasferisci @mario 100", username="mittente")

        await economy.cmd_trasferisci(message, session)

        assert await _coins(session, SENDER) == 500
        assert "Wallet non trovato" in message.said

    async def test_the_history_records_who_the_counterparty_was(
        self, session, user_factory
    ):
        """The two ledger rows carry the other party's name in their description —
        that is the only place `/storico` can read it from, so an anonymous transfer
        would leave both sides unable to tell what happened."""
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)

        await economy.cmd_trasferisci(
            _FakeMessage("/trasferisci @mario 100", username="mittente"), session
        )

        descriptions = " ".join(e.description or "" for e in await _ledger(session))
        assert "mario" in descriptions and "mittente" in descriptions

    async def test_an_unreachable_group_does_not_undo_a_committed_transfer(
        self, session, user_factory, monkeypatch, seeded_session
    ):
        """Transfers unlock milestones (`transfers_made`), announced in the group after
        the commit. If that send fails — bot removed from the group, no rights — the
        coins have already moved, so an exception here would show the sender a failure
        for a transfer that happened, and invite them to send it again.

        The failure is injected at the *bot* rather than by replacing
        `announce_trophies`: the guard being tested lives inside that function, so
        stubbing it out would test the stub. The group id has to be set too, or the
        function returns before ever reaching the send.
        """
        monkeypatch.setattr(economy_group_registry, "get_group_id", lambda: -100123)
        attempted: list[bool] = []

        async def refuse_send(*a, **kw):
            attempted.append(True)
            raise RuntimeError("Forbidden: bot is not a member of the supergroup chat")

        monkeypatch.setattr(economy_group_registry, "send_group_message", refuse_send)

        badges = await _some_badge(seeded_session)
        assert badges, "no badge seeded — the announcement would be skipped entirely"

        async def award(db, tg_id):
            return badges

        monkeypatch.setattr(economy.badge_service, "check_and_award_milestones", award)

        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/trasferisci @mario 100", username="mittente")

        await economy.cmd_trasferisci(message, session)  # must not raise

        assert attempted, "the send was never reached, so nothing was proven"
        assert await _coins(session, TARGET) == 100
        assert "100" in message.said


class TestDaily:
    async def test_a_claim_pays_and_reports_the_streak(self, session, user_factory):
        await user_factory(tg_id=SENDER, coins=0)
        message = _FakeMessage("/daily")

        await economy.cmd_daily(message, session)

        assert await _coins(session, SENDER) > 0
        assert "Streak" in message.said

    async def test_claiming_twice_is_refused_with_a_countdown(
        self, session, user_factory
    ):
        await user_factory(tg_id=SENDER, coins=0)
        await economy.cmd_daily(_FakeMessage("/daily"), session)
        paid_once = await _coins(session, SENDER)

        second = _FakeMessage("/daily")
        await economy.cmd_daily(second, session)

        assert await _coins(session, SENDER) == paid_once
        assert "già riscosso" in second.said
        assert "Torna tra" in second.said, "a refusal with no countdown is just a no"

    async def test_a_user_without_a_wallet_is_told_to_register(self, session):
        message = _FakeMessage("/daily")

        await economy.cmd_daily(message, session)

        assert "/start" in message.said

    async def test_a_claim_that_levels_you_up_says_so(
        self, session, user_factory, monkeypatch
    ):
        """Levelling up and earning a rank are the payoff of the XP system, and the
        daily reply is where a player sees them. Both lines are conditional, so both
        can silently stop being printed."""
        monkeypatch.setattr(economy.settings, "xp_per_daily_claim", 100_000)
        monkeypatch.setattr(economy.settings, "xp_daily_participation_cap", 100_000)
        await user_factory(tg_id=SENDER, coins=0)
        message = _FakeMessage("/daily")

        await economy.cmd_daily(message, session)

        assert "Livello" in message.said
        assert "rango" in message.said.lower()

    async def test_in_a_group_the_details_go_to_private(self, session, user_factory):
        """Streak, XP and rank are personal. The public reply acknowledges the claim
        and nothing else; everything else is sent in a DM."""
        await user_factory(tg_id=SENDER, coins=0)
        message = _FakeMessage("/daily", chat_type="supergroup")

        await economy.cmd_daily(message, session)

        public = "\n".join(message.replies)
        assert "riscosso" in public
        assert "Streak" not in public, "the streak was published to the group"
        assert message.bot.sent and "Streak" in message.bot.sent[0][1]

    async def test_in_a_group_without_a_private_chat_it_links_to_one(
        self, session, user_factory
    ):
        """A user who never opened the bot cannot be DMed, and dumping the details in
        the group instead would defeat the split above — so they get a deep link."""
        await user_factory(tg_id=SENDER, coins=0)
        message = _FakeMessage("/daily", chat_type="supergroup", bot=_MuteBot())

        await economy.cmd_daily(message, session)

        public = "\n".join(message.replies)
        assert "riscosso" in public
        assert "?start=daily" in public
        assert "Streak" not in public


class TestBalanceAndHistory:
    async def test_the_balance_is_shown(self, session, user_factory):
        await user_factory(tg_id=SENDER, coins=1234)
        message = _FakeMessage("/saldo")

        await economy.show_saldo(message, session)

        assert "1,234" in message.said or "1234" in message.said

    async def test_a_missing_wallet_is_reported(self, session):
        message = _FakeMessage("/saldo")

        await economy.show_saldo(message, session)

        assert "/start" in message.said

    async def test_an_empty_history_says_so(self, session, user_factory):
        await user_factory(tg_id=SENDER, coins=0)
        message = _FakeMessage("/storico")

        await economy.show_storico(message, session)

        assert "Nessuna transazione" in message.said

    async def test_the_history_labels_each_kind_of_movement(
        self, session, user_factory
    ):
        """Every transaction type has a human label; one missing would print the
        generic «📌 Transazione» and leave the user guessing where coins went."""
        await user_factory(tg_id=SENDER, coins=0)
        await economy.economy_service.credit(
            session, SENDER, 500, TransactionType.daily_reward, "premio"
        )
        await economy.economy_service.debit(
            session, SENDER, 100, TransactionType.shop_purchase, "acquisto"
        )
        await session.commit()
        message = _FakeMessage("/storico")

        await economy.show_storico(message, session)

        assert "Premio giornaliero" in message.said
        assert "Acquisto negozio" in message.said
        assert "Transazione" not in message.said, "an unlabelled transaction type"

    async def test_every_transaction_type_has_a_label(self):
        """Checked against the enum rather than against a list written here, so adding
        a type without a label fails immediately instead of at the user's screen."""
        missing = [t.value for t in TransactionType if t.value not in economy._TX_LABELS]
        assert missing == [], f"transaction types with no label: {missing}"

    async def test_a_transfer_shows_the_counterparty_in_the_history(
        self, session, user_factory
    ):
        await user_factory(tg_id=SENDER, username="mittente", coins=500)
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        await economy.cmd_trasferisci(
            _FakeMessage("/trasferisci @mario 100", username="mittente"), session
        )

        message = _FakeMessage("/storico")
        await economy.show_storico(message, session)

        assert "mario" in message.said


class TestAdminCredit:
    async def test_it_credits_and_names_the_target(self, session, user_factory):
        await user_factory(tg_id=SENDER, username="admin")
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/credita @mario 250")

        await economy.cmd_credita(message, session)

        assert await _coins(session, TARGET) == 250
        assert "mario" in message.said

    async def test_every_malformed_command_credits_nothing(self, session, user_factory):
        await user_factory(tg_id=SENDER, username="admin")
        await user_factory(tg_id=TARGET, username="mario", coins=0)

        for text in (
            "/credita",
            "/credita @mario",
            "/credita @mario tanti",
            "/credita @mario 0",
            "/credita @mario -50",
            "/credita @mario 10000001",   # above the cap
            "/credita @nessuno 50",
        ):
            message = _FakeMessage(text)
            await economy.cmd_credita(message, session)
            assert message.said, f"no feedback for: {text}"

        assert await _coins(session, TARGET) == 0

    async def test_the_target_can_be_given_as_a_numeric_id(self, session, user_factory):
        await user_factory(tg_id=SENDER, username="admin")
        await user_factory(tg_id=TARGET, username=None, coins=0)
        message = _FakeMessage(f"/credita {TARGET} 250")

        await economy.cmd_credita(message, session)

        assert await _coins(session, TARGET) == 250

    async def test_a_failure_is_reported_rather_than_raised(
        self, session, user_factory, monkeypatch
    ):
        """The catch-all here exists so a broken credit answers the admin instead of
        bubbling into the error handler with no context."""
        async def boom(*a, **kw):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(economy.economy_service, "credit", boom)
        await user_factory(tg_id=SENDER, username="admin")
        await user_factory(tg_id=TARGET, username="mario", coins=0)
        message = _FakeMessage("/credita @mario 250")

        await economy.cmd_credita(message, session)

        assert "fallita" in message.said


class TestCommandGuards:
    """The two entry points around `show_saldo` / `show_storico`."""

    def teardown_method(self):
        cooldown.reset()

    async def test_saldo_answers_in_the_group_but_only_once_per_window(
        self, session, user_factory
    ):
        """`/saldo` is public and answers in the group on purpose, so the throttle
        is what keeps one person from filling the chat with it. It is the silent
        variant: a «slow down» notice would be the flood itself."""
        cooldown.reset()
        await user_factory(tg_id=SENDER, username="tizio", coins=100)
        first = _FakeMessage("/saldo", chat_type="supergroup", username="tizio")
        second = _FakeMessage("/saldo", chat_type="supergroup", username="tizio")

        await economy.cmd_saldo(first, session)
        await economy.cmd_saldo(second, session)

        assert first.said and second.said == ""

    async def test_storico_never_answers_in_the_group(self, session, user_factory):
        """A transaction history is the clearest picture of what someone does with
        their coins; in the group it is readable by everyone (§9)."""
        cooldown.reset()
        await user_factory(tg_id=SENDER, username="tizio", coins=100)
        message = _FakeMessage("/storico", chat_type="supergroup", username="tizio")

        await economy.cmd_storico(message, session)

        assert "privata" in message.said.lower()

    async def test_storico_in_private_shows_the_history(self, session, user_factory):
        cooldown.reset()
        await user_factory(tg_id=SENDER, username="tizio", coins=100)
        message = _FakeMessage("/storico", username="tizio")

        await economy.cmd_storico(message, session)

        assert message.said
