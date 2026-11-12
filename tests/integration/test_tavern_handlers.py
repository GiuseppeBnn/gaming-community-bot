"""Locanda UX and privacy against real SQL, with Telegram transport stubbed."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from database.models import TavernOffer
from handlers import tavern
from handlers.callbacks import TavernCb
from services import consumable_service as stock, group_registry, tavern_service as gifts
from utils.daytime import utc_now

pytestmark = pytest.mark.asyncio
GROUP = -100123
KEY = "cons_nuka_cola"


def message(*, uid=1, chat_id=1, chat_type="private", target=None, bot=None, mid=10):
    bot = bot or SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid, is_bot=False), sender_chat=None,
        chat=SimpleNamespace(id=chat_id, type=chat_type), message_id=mid,
        reply_to_message=target, bot=bot, answer=AsyncMock(), reply=AsyncMock(),
        edit_text=AsyncMock(), edit_reply_markup=AsyncMock(),
    )


def callback(action, *, uid=1, ref=0, offer_id=0, msg=None):
    msg = msg or message()
    data = TavernCb(action=action, ref=ref, offer_id=offer_id)
    return SimpleNamespace(
        message=msg, bot=msg.bot, from_user=SimpleNamespace(id=uid),
        answer=AsyncMock(), payload=data, data=data.pack(),
    )


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    group_registry.set_runtime_group_id(GROUP)
    monkeypatch.setattr(tavern.cooldown, "guard", AsyncMock(return_value=True))
    yield
    group_registry.set_runtime_group_id(None)


@pytest.fixture
async def accounts(user_factory):
    for uid in (1, 2, 3):
        await user_factory(tg_id=uid, username=f"user{uid}", coins=2000, onboarding_completed=True)


async def offer(session, *, prepared=False, delivered=False, unit=None):
    row = await gifts.create_offer(session, 1, 2, GROUP, 10)
    if prepared or delivered:
        row = await gifts.prepare_offer(
            session, row.id, 1, GROUP, item_key=KEY if unit is None else None, source_purchase_id=unit,
        )
    if delivered:
        row, _ = await gifts.deliver_offer(session, row.id, 1, GROUP)
    await session.commit()
    return row


async def call(cb, session, handler=tavern.cb_offer):
    await handler(cb, cb.payload, session)
    return cb


async def test_group_command_creates_personal_private_invitation_without_payment(session, accounts):
    msg = message(chat_id=GROUP, chat_type="supergroup", target=message(uid=2))
    await tavern.cmd_offri(msg, session)
    row = (await gifts.recent_offers(session, 1))[0]
    assert row.status == "draft" and row.cost == 0
    assert await stock.purchase_counts(session, 1) == {}
    url = msg.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url
    assert url.endswith(f"start=offri_{row.token}")
    assert "2000" not in msg.reply.call_args.args[0]


@pytest.mark.parametrize("kind", ["private", "other_group", "no_target", "self", "bot", "anonymous", "anonymous_target", "not_registered", "cooldown", "unconfigured"])
async def test_command_rejects_invalid_contexts_without_gifts(session, accounts, monkeypatch, kind):
    msg = message(chat_id=GROUP, chat_type="supergroup", target=message(uid=2))
    if kind == "private":
        msg.chat.type = "private"
    elif kind == "other_group":
        msg.chat.id = -999
    elif kind == "no_target":
        msg.reply_to_message = None
    elif kind == "self":
        msg.reply_to_message.from_user.id = 1
    elif kind == "bot":
        msg.reply_to_message.from_user.is_bot = True
    elif kind == "anonymous":
        msg.sender_chat = SimpleNamespace(id=GROUP)
    elif kind == "anonymous_target":
        msg.reply_to_message.sender_chat = SimpleNamespace(id=GROUP)
    elif kind == "not_registered":
        msg.reply_to_message.from_user.id = 99
    elif kind == "cooldown":
        monkeypatch.setattr(tavern.cooldown, "guard", AsyncMock(return_value=False))
    elif kind == "unconfigured":
        group_registry.set_runtime_group_id(0)
    await tavern.cmd_offri(msg, session)
    assert await gifts.recent_offers(session, 1) == []


async def test_private_landing_authorization_and_group_redirect(session, accounts):
    row = await offer(session)
    msg = message()
    await tavern.start_offer_private(msg, session, row.token)
    assert "Cosa vuoi offrire" in msg.answer.call_args.args[0]
    stranger = message(uid=3)
    await tavern.start_offer_private(stranger, session, row.token)
    assert "personale" in stranger.answer.call_args.args[0]
    group = message(chat_id=GROUP, chat_type="supergroup")
    await tavern.start_offer_private(group, session, row.token)
    assert group.reply.await_count == 1


async def test_start_deeplink_dispatches_to_tavern_without_touching_other_fsm(session, accounts, monkeypatch):
    from aiogram.filters.command import CommandObject
    from handlers import common
    from handlers.guess import play

    row = await offer(session)
    monkeypatch.setattr(common, "_ensure_welcome_trophy", AsyncMock())
    monkeypatch.setattr(play, "leave_answering", AsyncMock())
    msg = message()
    state = SimpleNamespace(clear=AsyncMock())
    await common.cmd_start(msg, CommandObject(command="start", args=f"offri_{row.token}"), state, session)
    assert "Cosa vuoi offrire" in msg.answer.call_args.args[0]
    state.clear.assert_not_awaited()


async def test_menu_paging_selection_and_frozen_confirmation(session, accounts):
    row = await offer(session)
    cb = await call(callback("menu", offer_id=row.id), session)
    buttons = cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert any("Successivi" in button.text for line in buttons for button in line)
    cb = await call(callback("menu", offer_id=row.id, ref=999), session)
    assert "Precedenti" in str(cb.message.edit_text.call_args)
    cb = await call(callback("choose_buy", offer_id=row.id, ref=0), session)
    selected = await gifts.get_offer(session, row.id)
    assert selected.item_key == next(iter(stock.get_consumables()))
    assert "Conferma offerta" in str(cb.message.edit_text.call_args)
    await call(callback("choose_buy", offer_id=row.id, ref=1), session)
    assert (await gifts.get_offer(session, row.id)).item_key == selected.item_key
    cb = await call(callback("menu", offer_id=row.id), session)
    assert "Costo" in cb.message.edit_text.call_args.args[0]


async def test_stock_selection_empty_stock_invalid_selection_and_cancel(session, accounts):
    row = await offer(session)
    cb = await call(callback("stock", offer_id=row.id), session)
    assert "vuota" in cb.message.edit_text.call_args.args[0]
    bad = await call(callback("choose_buy", offer_id=row.id, ref=-1), session)
    assert bad.answer.call_args.kwargs["show_alert"]
    purchase = await stock.record_consumption(session, 1, stock.get_item(KEY), 250)
    await session.commit()
    cb = await call(callback("stock", offer_id=row.id), session)
    assert "Nuka-Cola" in cb.message.edit_text.call_args.args[0]
    await call(callback("choose_stock", offer_id=row.id, ref=purchase.id), session)
    assert (await gifts.get_offer(session, row.id)).cost == 0
    cb = await call(callback("cancel", offer_id=row.id), session)
    assert "annullata" in cb.message.edit_text.call_args.args[0]


async def test_confirm_pays_once_publishes_and_reopens_receipt(session, accounts, monkeypatch):
    row = await offer(session, prepared=True)
    check = AsyncMock(return_value=[SimpleNamespace()])
    announce = AsyncMock()
    monkeypatch.setattr(tavern.badge_service, "check_and_award_milestones", check)
    monkeypatch.setattr(tavern, "announce_trophies", announce)
    cb = await call(callback("confirm", offer_id=row.id), session)
    receipt = await gifts.get_offer(session, row.id)
    assert receipt.status == "delivered" and receipt.message_id == 900
    assert cb.bot.get_chat_member.await_count == 2
    assert cb.bot.send_message.call_args.args[0] == GROUP
    assert "2000" not in cb.bot.send_message.call_args.args[1]
    assert "Conferma" not in cb.message.edit_text.call_args.args[0]
    announce.assert_awaited_once()
    await call(callback("confirm", offer_id=row.id), session)
    assert await stock.purchase_counts(session, 1) == {KEY: 1}
    check.assert_awaited_once()
    cb = await call(callback("open", offer_id=row.id), session)
    assert "già nella sua dispensa" in cb.message.edit_text.call_args.args[0]


async def test_stock_confirmation_does_not_award_purchase_trophies(session, accounts, monkeypatch):
    unit = await stock.record_consumption(session, 1, stock.get_item(KEY), 250)
    await session.commit()
    row = await offer(session, prepared=True, unit=unit.id)
    check = AsyncMock()
    monkeypatch.setattr(tavern.badge_service, "check_and_award_milestones", check)
    await call(callback("confirm", offer_id=row.id), session)
    check.assert_not_awaited()
    assert await stock.stock_counts(session, 2) == {KEY: 1}


@pytest.mark.parametrize("kind", ["outsider", "missing", "membership", "member_network", "balance"])
async def test_confirmation_failures_never_charge_or_publish(session, accounts, kind):
    row = await offer(session, prepared=True)
    cb = callback("confirm", offer_id=row.id)
    if kind == "outsider":
        cb.from_user.id = 3
    elif kind == "missing":
        cb.payload = TavernCb(action="confirm", offer_id=-1)
    elif kind == "membership":
        cb.bot.get_chat_member.return_value.status = "left"
    elif kind == "member_network":
        cb.bot.get_chat_member.side_effect = RuntimeError("private transport error")
    elif kind == "balance":
        from database.models import Wallet
        await session.execute(update(Wallet).where(Wallet.tg_id == 1).values(coins=1))
        await session.commit()
    await call(cb, session)
    if kind in {"membership", "member_network", "balance"}:
        cb.message.answer.assert_awaited_once()
        cb.answer.assert_awaited_once_with("Verifico l'offerta…")
    else:
        assert cb.answer.call_args.kwargs["show_alert"]
    assert await stock.purchase_counts(session, 1) == {}
    cb.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("status,is_member,result", [("member", False, True), ("administrator", False, True), ("creator", False, True), ("restricted", True, True), ("restricted", False, False), ("left", False, False)])
async def test_membership_policy(status, is_member, result):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status, is_member=is_member)))
    assert await tavern._member(bot, 1) is result


async def test_failed_publication_is_retryable_without_rebuy_and_fenced(session, accounts, caplog):
    row = await offer(session, delivered=True)
    cb = callback("publish", offer_id=row.id)
    cb.bot.send_message.side_effect = RuntimeError("SECRET_PROVIDER_BODY")
    await call(cb, session)
    assert "SECRET_PROVIDER_BODY" not in caplog.text
    assert (await gifts.get_offer(session, row.id)).message_id is None
    assert "Pubblica nel gruppo" in str(cb.message.edit_text.call_args)
    retry = await call(callback("publish", offer_id=row.id), session)
    assert (await gifts.get_offer(session, row.id)).message_id == 900
    assert await stock.purchase_counts(session, 1) == {KEY: 1}
    retry.bot.send_message.assert_awaited_once()
    again = await call(callback("publish", offer_id=row.id), session)
    again.bot.send_message.assert_not_awaited()


async def test_publish_before_confirmation_rejected_and_expiry_rendering(session, accounts):
    row = await offer(session)
    cb = await call(callback("publish", offer_id=row.id), session)
    assert cb.answer.call_args.kwargs["show_alert"]
    await session.execute(update(TavernOffer).where(TavernOffer.id == row.id).values(expires_at=utc_now() - timedelta(seconds=1)))
    await session.commit()
    cb = await call(callback("open", offer_id=row.id), session)
    assert "scaduta" in cb.message.edit_text.call_args.args[0]
    cb = await call(callback("offers"), session, tavern.cb_personal)
    assert "scaduta" in str(cb.message.edit_text.call_args)


async def test_missing_catalog_labels_are_safe_in_old_receipts(session, accounts):
    row = await offer(session, delivered=True)
    missing = replace(row, item_key="removed")
    msg = message()
    await tavern.show_offer(msg, session, missing)
    assert "un oggetto" in msg.answer.call_args.args[0]
    pending = replace(missing, status="prepared")
    await tavern.show_offer(msg, session, pending)
    assert "non più disponibile" in msg.answer.call_args.args[0]


async def test_pantry_pages_and_exact_unit_consumption_replay(session, accounts):
    for item in stock.get_consumables().values():
        await stock.record_consumption(session, 1, item, item.price)
    await session.commit()
    cb = await call(callback("pantry"), session, tavern.cb_personal)
    assert "Successivi" in str(cb.message.edit_text.call_args)
    cb = await call(callback("pantry", ref=999), session, tavern.cb_personal)
    assert "Precedenti" in str(cb.message.edit_text.call_args)
    unit = (await stock.stock_units(session, 1))[KEY]
    cb = await call(callback("consume", ref=unit), session, tavern.cb_personal)
    assert "Consumato" in cb.answer.call_args.args[0]
    cb = await call(callback("consume", ref=unit), session, tavern.cb_personal)
    assert cb.answer.call_args.kwargs["show_alert"]
    cb = await call(callback("pantry", uid=2), session, tavern.cb_personal)
    assert "vuota" in cb.message.edit_text.call_args.args[0]


@pytest.mark.parametrize("kind", ["drink", "keep", "wrong_user", "wrong_unit", "wrong_message", "wrong_group", "already_consumed", "missing_offer"])
async def test_only_recipient_can_consume_or_keep_the_exact_published_gift(session, accounts, kind):
    row = await offer(session, delivered=True)
    await session.execute(update(TavernOffer).where(TavernOffer.id == row.id).values(message_id=900))
    await session.commit()
    msg = message(chat_id=GROUP, chat_type="supergroup", mid=900)
    cb = callback("keep" if kind == "keep" else "drink", uid=2, offer_id=row.id, ref=row.purchase_id, msg=msg)
    if kind == "wrong_user":
        cb.from_user.id = 3
    elif kind == "wrong_unit":
        cb.payload = replace_payload(cb.payload, ref=row.purchase_id + 1)
    elif kind == "wrong_message":
        msg.message_id = 899
    elif kind == "wrong_group":
        msg.chat.id = -999
    elif kind == "already_consumed":
        await stock.consume_unit(session, row.purchase_id, 2)
        await session.commit()
    elif kind == "missing_offer":
        cb.payload = replace_payload(cb.payload, offer_id=-1)
    await call(cb, session, tavern.cb_receive)
    if kind == "drink":
        assert await stock.stock_counts(session, 2) == {}
        msg.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    elif kind == "keep":
        assert await stock.stock_counts(session, 2) == {KEY: 1}
        msg.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    else:
        assert cb.answer.call_args.kwargs["show_alert"]


def replace_payload(payload, **updates):
    return payload.model_copy(update=updates)


async def test_callback_wire_budget_and_private_filters():
    payload = TavernCb(action="choose_stock", ref=2_147_483_647, offer_id=2_147_483_647)
    assert len(payload.pack().encode()) < 64
    assert TavernCb.unpack(payload.pack()) == payload
    # A copied personal confirmation cannot run from a shared group panel.
    from aiogram.types import CallbackQuery, Chat, Message, User
    real = CallbackQuery(
        id="test", chat_instance="test", from_user=User(id=1, is_bot=False, first_name="A"),
        data=TavernCb(action="confirm", offer_id=1).pack(),
        message=Message(message_id=1, date=utc_now(), chat=Chat(id=GROUP, type="supergroup")),
    )
    for handler in tavern.router.callback_query.handlers:
        if handler.callback is tavern.cb_offer:
            allowed, _ = await handler.check(real)
            assert not allowed


async def test_slow_membership_is_acknowledged_before_network_without_open_transaction(session, accounts):
    row = await offer(session, prepared=True)
    cb = callback("confirm", offer_id=row.id)
    async def membership(*args, **kwargs):
        cb.answer.assert_awaited_once_with("Verifico l'offerta…")
        assert not session.in_transaction()
        return SimpleNamespace(status="member")
    cb.bot.get_chat_member.side_effect = membership
    await call(cb, session)
    assert (await gifts.get_offer(session, row.id)).status == "delivered"


@pytest.mark.parametrize("cosmetic", [False, True])
async def test_normal_purchases_lock_accounts_before_debit(session, accounts, monkeypatch, cosmetic):
    from handlers import shop
    from handlers.callbacks import ShopCb

    events = []
    real_lock = shop.economy_service.lock_users_then_wallets
    real_debit = shop.economy_service.debit
    async def lock(*args, **kwargs):
        events.append("accounts")
        return await real_lock(*args, **kwargs)
    async def debit(*args, **kwargs):
        assert events == ["accounts"]
        events.append("debit")
        return await real_debit(*args, **kwargs)
    monkeypatch.setattr(shop.economy_service, "lock_users_then_wallets", lock)
    monkeypatch.setattr(shop.economy_service, "debit", debit)
    cb = callback("unused")
    data = ShopCb(action="exec" if cosmetic else "cexec", key="tag_memelord" if cosmetic else KEY)
    handler = shop.cb_shop_execute if cosmetic else shop.cb_consumable_execute
    await handler(cb, data, session)
    assert events == ["accounts", "debit"]
