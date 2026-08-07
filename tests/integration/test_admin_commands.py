"""The `/`-command half of the admin surface — `handlers/admin.py`.

The dashboard equivalents are covered in `test_admin_dashboard_money.py`, and they
share the service layer. What is *not* shared is everything above it: these commands
take their target and their amount out of free text typed into a group, through
`resolve_target` (covered in `tests/unit/test_targeting.py`) and `_split_amount`.

So this file does not re-test what a debit does. It tests what the command decides:
who, how much, and — for moderation — whether it is allowed at all. Plus the two
guards that only exist here, and only in the command path:

  * `/ban` applies the bot-level ban **even when Telegram refuses** to remove the
    user from the group, because otherwise a group admin, or someone who already
    left, would keep using the bot in private;
  * `/mute 10m spam` and `/mute spam` both have to work — the first token is a
    duration only if it looks like one, otherwise it is the start of the reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import AdminAction, User, Wallet
from handlers import admin

ADMIN_ID = 1
TARGET_ID = 42
GROUP_ID = -100123


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.banned: list[int] = []
        self.unbanned: list[int] = []
        self.restricted: list[tuple[int, int | None]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append(user_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
        self.restricted.append((user_id, until_date))

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _RefusingBot(_FakeBot):
    """Telegram refuses every group action — target is an admin, or no rights."""

    async def ban_chat_member(self, chat_id, user_id, **kw):
        raise RuntimeError("Bad Request: user is an administrator of the chat")

    async def unban_chat_member(self, chat_id, user_id, **kw):
        raise RuntimeError("Bad Request: not enough rights")

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
        raise RuntimeError("Bad Request: not enough rights")


class _MuteBot(_FakeBot):
    """The target never opened the bot in private, so no DM can reach them."""

    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("Forbidden: bot can't initiate conversation with a user")


class _FakeMessage:
    def __init__(self, text: str = "", *, bot=None, chat_type: str = "supergroup") -> None:
        self.text = text
        self.entities: list = []
        self.reply_to_message = None
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(
            id=GROUP_ID if chat_type != "private" else ADMIN_ID, type=chat_type
        )
        self.replies: list[str] = []
        self.answers: list[str] = []

    async def reply(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies))

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=len(self.answers))

    @property
    def said(self) -> str:
        return "\n".join(self.replies + self.answers)


def _cmd(args: str | None):
    return SimpleNamespace(args=args)


async def _run(handler, args: str | None, *, session, bot=None, chat_type="supergroup"):
    message = _FakeMessage(bot=bot, chat_type=chat_type)
    await handler(message, _cmd(args), session)
    return message


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _xp(session, tg_id: int) -> int:
    return (await session.execute(select(User.xp).where(User.tg_id == tg_id))).scalar_one()


async def _audit(session) -> list[AdminAction]:
    return list((await session.execute(select(AdminAction))).scalars().all())


class TestCurrencyCommands:
    async def test_addebita_takes_the_amount_and_keeps_the_reason(
        self, session, user_factory
    ):
        """`/addebita @mario 100 spam nel gruppo` — the first token after the target is
        the amount, everything after it is the reason, and the reason is what ends up
        in the ledger description instead of the generic fallback."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=500)

        message = await _run(admin.cmd_addebita, "@mario 100 spam nel gruppo", session=session)

        assert await _coins(session, TARGET_ID) == 400
        [row] = await _audit(session)
        assert row.action_type == "addebita" and row.amount == -100
        assert row.detail == "spam nel gruppo"
        assert "mario" in message.said

    async def test_addebita_refuses_more_than_the_balance(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=50)

        message = await _run(admin.cmd_addebita, "@mario 100", session=session)

        assert await _coins(session, TARGET_ID) == 50
        assert await _audit(session) == []
        assert "50" in message.said

    async def test_setsaldo_lands_on_the_number_and_records_the_jump(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=9999)

        await _run(admin.cmd_setsaldo, "@mario 50", session=session)

        assert await _coins(session, TARGET_ID) == 50
        [row] = await _audit(session)
        assert row.detail == "9999 → 50"

    async def test_setsaldo_accepts_zero_but_addebita_does_not(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=700)

        await _run(admin.cmd_setsaldo, "@mario 0", session=session)
        assert await _coins(session, TARGET_ID) == 0

        refused = await _run(admin.cmd_addebita, "@mario 0", session=session)
        assert "Uso:" in refused.said

    async def test_currency_commands_refuse_an_unregistered_target(self, session):
        """Resolving by id succeeds for someone the bot has never seen, but there is no
        wallet to move coins in or out of — so this has to be caught before the debit,
        not by an exception from it."""
        for handler in (admin.cmd_addebita, admin.cmd_setsaldo, admin.cmd_dai_xp,
                        admin.cmd_set_xp, admin.cmd_saldo_di):
            message = await _run(handler, "4242 100", session=session)
            assert "non è registrato" in message.said or "non registrato" in message.said

    async def test_currency_commands_ask_for_a_target_when_given_none(self, session):
        for handler in (admin.cmd_addebita, admin.cmd_setsaldo, admin.cmd_dai_xp,
                        admin.cmd_set_xp, admin.cmd_saldo_di):
            message = await _run(handler, None, session=session)
            assert "Specifica un utente" in message.said

    async def test_a_non_numeric_amount_moves_nothing(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=500)

        for args in ("@mario", "@mario tanti", "@mario -5"):
            message = await _run(admin.cmd_addebita, args, session=session)
            assert "Uso:" in message.said, args

        assert await _coins(session, TARGET_ID) == 500

    async def test_dai_xp_adds_and_set_xp_replaces(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", xp=100)

        await _run(admin.cmd_dai_xp, "@mario 400", session=session)
        assert await _xp(session, TARGET_ID) == 500

        await _run(admin.cmd_set_xp, "@mario 10", session=session)
        assert await _xp(session, TARGET_ID) == 10

        assert [r.action_type for r in await _audit(session)] == ["xp_grant", "xp_set"]

    async def test_a_grant_that_levels_the_user_up_says_so(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", xp=0)

        message = await _run(admin.cmd_dai_xp, "@mario 100000", session=session)

        assert "Livello" in message.said and "rango" in message.said.lower()

    async def test_airdrop_pays_everyone_and_records_how_many(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin", coins=0)
        for tg_id in (10, 11):
            await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=100)

        message = await _run(admin.cmd_airdrop, "50 regalo di natale", session=session)

        assert [await _coins(session, i) for i in (10, 11)] == [150, 150]
        [row] = await _audit(session)
        assert row.action_type == "airdrop" and row.amount == 50
        assert "3 utenti" in row.detail and "regalo di natale" in row.detail
        assert "3" in message.said

    async def test_an_invalid_airdrop_pays_nobody(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin", coins=0)
        await user_factory(tg_id=10, username="u10", coins=100)

        for args in (None, "", "zero", "0", "-5"):
            message = await _run(admin.cmd_airdrop, args, session=session)
            assert "Uso:" in message.said, args

        assert await _coins(session, 10) == 100

    async def test_saldo_di_reports_the_balance(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=777)

        message = await _run(admin.cmd_saldo_di, "@mario", session=session)

        assert "777" in message.said


class TestModeration:
    async def test_ban_marks_the_user_and_tells_them(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        bot = _FakeBot()

        message = await _run(admin.cmd_ban, "@mario flood", session=session, bot=bot)

        assert bot.banned == [TARGET_ID]
        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is True
        [row] = await _audit(session)
        assert row.action_type == "ban" and row.detail == "flood"
        assert bot.sent and bot.sent[0][0] == TARGET_ID
        assert "flood" in message.said

    async def test_a_refused_group_removal_still_bans_from_the_bot(
        self, session, user_factory
    ):
        """The guarantee that makes `/ban` meaningful: Telegram refuses to remove group
        admins, and a user who already left cannot be removed at all. If the bot-level
        ban depended on that call succeeding, both would keep using the bot in private
        after being banned."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        message = await _run(admin.cmd_ban, "@mario", session=session, bot=_RefusingBot())

        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is True
        assert "non riuscita" in message.said, "the admin was not warned"

    async def test_sban_lifts_both_the_group_ban_and_the_bot_ban(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        await _run(admin.cmd_ban, "@mario", session=session)

        bot = _FakeBot()
        await _run(admin.cmd_sban, "@mario", session=session, bot=bot)

        assert bot.unbanned == [TARGET_ID]
        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is False

    async def test_a_failed_sban_leaves_the_bot_ban_in_place(
        self, session, user_factory
    ):
        """The opposite asymmetry from `/ban`, and deliberately so: un-banning is a
        removal of a restriction, so if Telegram refuses, nothing should be relaxed
        halfway."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        await _run(admin.cmd_ban, "@mario", session=session)

        await _run(admin.cmd_sban, "@mario", session=session, bot=_RefusingBot())

        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is True

    async def test_kick_removes_and_lets_them_return(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        bot = _FakeBot()

        await _run(admin.cmd_kick, "@mario", session=session, bot=bot)

        assert bot.banned == [TARGET_ID] and bot.unbanned == [TARGET_ID]
        assert [r.action_type for r in await _audit(session)] == ["kick"]

    async def test_a_failed_kick_is_not_logged(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        message = await _run(admin.cmd_kick, "@mario", session=session, bot=_RefusingBot())

        assert await _audit(session) == [], "an expulsion that never happened was logged"
        assert message.said

    async def test_mute_reads_a_leading_duration(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        bot = _FakeBot()

        message = await _run(admin.cmd_mute, "@mario 10m troppo rumore", session=session, bot=bot)

        [row] = await _audit(session)
        assert row.amount == 600, "the duration token was not parsed"
        assert row.detail == "troppo rumore"
        assert "10 min" in message.said

    async def test_mute_without_a_duration_uses_the_default_and_keeps_the_reason(
        self, session, user_factory
    ):
        """`/mute @mario spam` — «spam» must not be mistaken for a duration, and must
        not be swallowed either. This is the ambiguity the command lives with."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        await _run(admin.cmd_mute, "@mario spam", session=session)

        [row] = await _audit(session)
        assert row.amount > 0
        assert row.detail == "spam"

    async def test_a_failed_mute_is_not_logged(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        await _run(admin.cmd_mute, "@mario 10m", session=session, bot=_RefusingBot())

        assert await _audit(session) == []

    async def test_unmute_lifts_the_restriction(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        bot = _FakeBot()

        await _run(admin.cmd_unmute, "@mario", session=session, bot=bot)

        assert bot.restricted and bot.restricted[0][0] == TARGET_ID
        assert [r.action_type for r in await _audit(session)] == ["unmute"]

    async def test_a_failed_unmute_is_not_logged(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        await _run(admin.cmd_unmute, "@mario", session=session, bot=_RefusingBot())

        assert await _audit(session) == []

    async def test_moderating_yourself_is_refused(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        for handler in (admin.cmd_ban, admin.cmd_kick, admin.cmd_mute, admin.cmd_warn):
            message = await _run(handler, "@admin", session=session)
            assert "te stesso" in message.said, handler.__name__

        assert await _audit(session) == []

    async def test_moderating_the_bot_is_refused(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        for handler in (admin.cmd_ban, admin.cmd_kick, admin.cmd_mute, admin.cmd_warn):
            message = await _run(handler, str(_FakeBot.id), session=session)
            assert "me stesso" in message.said, handler.__name__

    async def test_in_private_without_a_group_moderation_is_refused(
        self, session, user_factory, monkeypatch
    ):
        """In a group the chat itself is the target. In private there is no chat to
        moderate, so the configured GROUP_ID is the only option — and without it the
        call would go to Telegram with a zero chat id."""
        monkeypatch.setattr(admin.group_registry, "get_group_id", lambda: 0)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        for handler in (admin.cmd_ban, admin.cmd_sban, admin.cmd_kick,
                        admin.cmd_mute, admin.cmd_unmute):
            message = await _run(handler, "@mario", session=session, chat_type="private")
            assert "GROUP_ID" in message.said, handler.__name__

    async def test_a_target_who_blocked_the_bot_is_still_moderated(
        self, session, user_factory
    ):
        """The courtesy DM is best-effort; the ban is not."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        await _run(admin.cmd_ban, "@mario", session=session, bot=_MuteBot())

        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is True


class TestWarns:
    async def test_a_warn_counts_and_reports_the_threshold(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        message = await _run(admin.cmd_warn, "@mario spam", session=session)

        [row] = [r for r in await _audit(session) if r.action_type == "warn"]
        assert row.amount == 1 and row.detail == "spam"
        assert f"1/{admin.settings.warn_ban_threshold}" in message.said

    async def test_crossing_the_ban_threshold_bans(
        self, session, user_factory, monkeypatch
    ):
        """The escalation is the reason warns exist. It has to fire from the command
        path exactly as it does from the dashboard — same helper, but only a test
        proves the command reaches it."""
        monkeypatch.setattr(admin.settings, "warn_mute_threshold", 2)
        monkeypatch.setattr(admin.settings, "warn_ban_threshold", 3)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        for _ in range(2):
            await _run(admin.cmd_warn, "@mario", session=session)
        message = await _run(admin.cmd_warn, "@mario", session=session)

        assert "BAN" in message.said
        assert await session.scalar(select(User.is_banned).where(User.tg_id == TARGET_ID)) is True

    async def test_warns_lists_them_and_says_when_there_are_none(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        empty = await _run(admin.cmd_warns, "@mario", session=session)
        assert "non ha warn attivi" in empty.said

        await _run(admin.cmd_warn, "@mario motivo preciso", session=session)
        listed = await _run(admin.cmd_warns, "@mario", session=session)
        assert "motivo preciso" in listed.said

    async def test_unwarn_removes_one_and_reports_the_rest(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")
        for _ in range(2):
            await _run(admin.cmd_warn, "@mario", session=session)

        message = await _run(admin.cmd_unwarn, "@mario", session=session)

        assert await admin.admin_service.active_warning_count(session, TARGET_ID) == 1
        assert "restano 1" in message.said

    async def test_unwarn_on_a_clean_user_says_so(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario")

        message = await _run(admin.cmd_unwarn, "@mario", session=session)

        assert "non ha warn attivi" in message.said
        assert await _audit(session) == []


class TestDossierAndReports:
    async def test_info_renders_the_dossier(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=555, xp=250)

        message = await _run(admin.cmd_info, "@mario", session=session)

        assert "555" in message.said and "250" in message.said
        assert "Stato gruppo" in message.said

    async def test_info_on_an_unregistered_user_says_so(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        message = await _run(admin.cmd_info, "4242", session=session)

        assert "non registrato" in message.said

    async def test_cerca_finds_and_reports_nothing_found(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="pippo", coins=5)

        found = await _run(admin.cmd_cerca, "pippo", session=session, chat_type="private")
        assert "pippo" in found.said

        missing = await _run(admin.cmd_cerca, "qwertyuiop", session=session, chat_type="private")
        assert "Nessun utente trovato" in missing.said

    async def test_cerca_needs_two_characters(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        message = await _run(admin.cmd_cerca, "p", session=session, chat_type="private")

        assert "almeno 2 caratteri" in message.said

    async def test_the_reports_render(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin", coins=10, xp=50)

        for handler in (admin.cmd_classifica, admin.cmd_stats):
            message = _FakeMessage()
            await handler(message, session)
            assert message.said, handler.__name__

        ranks = _FakeMessage()
        await admin.cmd_lista_ranghi(ranks)
        assert ranks.said and "Livelli" in ranks.said

    async def test_audit_renders_globally_and_per_user(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="mario", coins=500)
        await _run(admin.cmd_addebita, "@mario 100", session=session)

        every = await _run(admin.cmd_audit, None, session=session, chat_type="private")
        assert every.said

        just_mario = await _run(admin.cmd_audit, "@mario", session=session, chat_type="private")
        assert just_mario.said


# ---------------------------------------------------------------------------
# The refusals every command shares
# ---------------------------------------------------------------------------

class TestUnresolvableTarget:
    """`_resolve_or_warn` is the first line of every one of these commands. When it
    cannot name a user, each must stop *there* — a moderation command that carried
    on with `tg_id=None` would act on whoever the fallback happened to pick."""

    @pytest.mark.parametrize("handler", [
        "cmd_ban", "cmd_sban", "cmd_kick", "cmd_mute", "cmd_unmute",
        "cmd_warn", "cmd_warns", "cmd_unwarn", "cmd_info",
    ])
    async def test_a_command_with_no_target_explains_how_to_give_one(
        self, session, handler
    ):
        bot = _FakeBot()

        message = await _run(getattr(admin, handler), None, session=session, bot=bot)

        assert "Specifica un utente" in message.said
        assert bot.banned == [] and bot.restricted == [] and bot.unbanned == []
        assert await _audit(session) == []


class TestAmountValidation:
    @pytest.mark.parametrize("handler,args", [
        ("cmd_setsaldo", f"{TARGET_ID}"),
        ("cmd_setsaldo", f"{TARGET_ID} meta"),
        ("cmd_setsaldo", f"{TARGET_ID} -5"),
        ("cmd_dai_xp", f"{TARGET_ID}"),
        ("cmd_dai_xp", f"{TARGET_ID} 0"),
        ("cmd_set_xp", f"{TARGET_ID}"),
        ("cmd_set_xp", f"{TARGET_ID} -1"),
    ])
    async def test_an_unusable_amount_shows_the_usage_and_changes_nothing(
        self, session, user_factory, handler, args
    ):
        """These three write an absolute value straight into a wallet or an XP
        total; a loosely-parsed amount is a number nobody typed."""
        await user_factory(tg_id=TARGET_ID, username="vittima", coins=500, xp=100)

        message = await _run(getattr(admin, handler), args, session=session)

        assert "Uso:" in message.said
        assert await _coins(session, TARGET_ID) == 500
        assert await _xp(session, TARGET_ID) == 100

    async def test_a_user_row_without_a_wallet_is_reported_not_crashed(
        self, session
    ):
        """A User can exist without a Wallet (an old row, a partial import). The
        command has to say so — `set_balance` locks the wallet row and raises."""
        session.add(User(tg_id=TARGET_ID, username="vittima", full_name="Vittima"))
        await session.commit()

        message = await _run(admin.cmd_setsaldo, f"{TARGET_ID} 100", session=session)

        assert "⚠️" in message.said
        assert await _audit(session) == []


class TestPrivateOnlyCommands:
    @pytest.mark.parametrize("handler", ["cmd_cerca", "cmd_audit"])
    async def test_they_never_answer_in_the_group(self, session, user_factory, handler):
        """Both print other people's data — search results and the admin audit
        trail. In the group that is a leak, so they hand back a link instead."""
        await user_factory(tg_id=TARGET_ID, username="vittima", coins=500)

        message = await _run(getattr(admin, handler), "vittima", session=session)

        assert "privata" in message.said.lower()
        assert "vittima" not in message.said.replace("privata", "")


class TestDossier:
    async def test_a_telegram_lookup_that_fails_does_not_lose_the_dossier(
        self, session, user_factory
    ):
        """The group status line is a nicety fetched from Telegram; the rest of the
        dossier is local data the admin asked for."""
        await user_factory(tg_id=TARGET_ID, username="vittima", coins=500)

        class _NoStatusBot(_FakeBot):
            async def get_chat_member(self, chat_id, user_id):
                raise RuntimeError("Bad Request: user not found")

        message = await _run(
            admin.cmd_info, str(TARGET_ID), session=session,
            bot=_NoStatusBot(), chat_type="private",
        )

        assert "vittima" in message.said
        assert "Stato gruppo" not in message.said


class TestRendering:
    async def test_an_empty_leaderboard_says_so(self, session):
        assert "Nessun utente" in await admin.render_leaderboard(session)

    async def test_the_audit_line_labels_coins_and_xp_differently(
        self, session, user_factory
    ):
        """Reading «+500» in the audit without knowing whether it was coins or XP
        makes the log useless for the one thing it exists for."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await user_factory(tg_id=TARGET_ID, username="vittima", coins=0, xp=0)
        await _run(admin.cmd_setsaldo, f"{TARGET_ID} 500", session=session)
        await _run(admin.cmd_dai_xp, f"{TARGET_ID} 50", session=session)
        await _run(admin.cmd_ban, f"{TARGET_ID} spam", session=session)

        rendered = await admin.render_audit(session, None)

        assert "+500🪙" in rendered
        assert "+50 XP" in rendered
        assert "ban" in rendered.lower()
