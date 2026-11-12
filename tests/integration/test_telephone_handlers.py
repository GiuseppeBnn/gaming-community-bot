from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
import pytest
from sqlalchemy import select

from database.models import TelephoneVisit
from handlers import telephone as ui
from handlers.callbacks import TelephoneCb
from services import telephone_service as game

GROUP = -1001234


class Bot:
    def __init__(self):
        self.sent, self.edits = [], []
        self.member = "member"
        self.failure = None
        self.db = None

    def _network(self):
        assert self.db is None or not self.db.in_transaction(), "DB transaction held over Telegram"

    async def get_chat_member(self, *args, **kw):
        self._network()
        if self.failure == "member":
            raise RuntimeError("test")
        return SimpleNamespace(status=self.member, is_member=True)

    async def get_me(self):
        self._network()
        return SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, **kw):
        self._network()
        if self.failure == "message":
            raise RuntimeError("test")
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(
            message_id=len(self.sent), photo=[SimpleNamespace(file_id=f"file-{len(self.sent)}")]
        )

    async def send_photo(self, chat_id, photo, **kw):
        self._network()
        if self.failure == "photo":
            raise RuntimeError("test")
        return await self.send_message(chat_id, kw.get("caption", ""), photo=photo, **kw)

    async def edit_message_text(self, **kw):
        self._network()
        self.edits.append(kw)
        if self.failure == "edit":
            raise TelegramBadRequest(method=EditMessageText(text="test"), message="no permission")
        if self.failure == "same":
            raise TelegramBadRequest(
                method=EditMessageText(text="test"), message="message is not modified"
            )

    async def edit_message_media(self, **kw):
        self._network()
        self.edits.append(kw)


class Message:
    def __init__(self, bot, user=10, text="", reply_id=None, chat_type="private"):
        self.bot = bot
        self.chat = SimpleNamespace(id=user if chat_type == "private" else GROUP, type=chat_type)
        self.from_user = SimpleNamespace(id=user, full_name=f"User <{user}>")
        self.text = text
        self.reply_to_message = SimpleNamespace(message_id=reply_id)
        self.message_id = 1

    async def answer(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)

    async def edit_text(self, text, **kw):
        return await self.answer(text, **kw)


@pytest.fixture
def setup(monkeypatch, session):
    monkeypatch.setattr(game.settings, "telephone_enabled", True)
    monkeypatch.setattr(game.settings, "openrouter_api_key", "fake")
    monkeypatch.setattr(ui.group_registry, "get_group_id", lambda: GROUP)
    generator = AsyncMock(return_value=b"test-image")
    monkeypatch.setattr(ui.image_service, "generate", generator)
    bot = Bot()
    bot.db = session
    return (
        bot,
        SimpleNamespace(get_state=AsyncMock(return_value=None), clear=AsyncMock()),
        generator,
    )


async def running(session):
    game_id = await game.create(session, 1)
    assert await game.start(session, game_id, GROUP)
    await session.commit()
    return game_id


async def prompt_id(session, game_id, user):
    return await session.scalar(
        select(TelephoneVisit.prompt_message_id).where(
            TelephoneVisit.session_id == game_id, TelephoneVisit.user_tg_id == user
        )
    )


async def test_complete_private_relay_and_public_gallery(session, setup):
    bot, state, generator = setup
    game_id = await running(session)
    for user in range(10, 14):
        await ui.join(Message(bot, user), state, session, game_id)
        prompt = await prompt_id(session, game_id, user)
        await ui.reply(Message(bot, user, f"scene {user}", prompt), session)
        assert generator.call_args.args == (f"scene {user}",)
    view = await game.get_view(session, game_id)
    assert view.status == "finished" and view.anchor is not None
    assert generator.await_count == 4
    assert sum(chat == GROUP for chat, _, _ in bot.sent) == 2  # one card + one final notification
    assert "media" in bot.edits[-1]
    assert "&lt;10&gt;" in bot.edits[-1]["media"].caption
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=GROUP), message_id=view.anchor),
        bot=bot,
        answer=AsyncMock(),
    )
    await ui.gallery(callback, TelephoneCb(action="view", game_id=game_id, stage=4), session)
    assert "4/4" in bot.edits[-1]["media"].caption
    await ui.gallery(callback, TelephoneCb(action="view", game_id=game_id, stage=2), session)
    assert len(bot.edits[-1]["reply_markup"].inline_keyboard[0]) == 2
    await ui.gallery(callback, TelephoneCb(action="view", game_id=game_id, stage=99), session)
    assert "non disponibile" in callback.answer.call_args.args[0]


async def test_creation_is_admin_entry_and_disabled_error_is_clear(session, setup, monkeypatch):
    bot, state, _ = setup
    cb = SimpleNamespace(
        message=Message(bot, 1), from_user=SimpleNamespace(id=1), answer=AsyncMock()
    )
    await ui.create(cb, state, session)
    assert len(await game.list_games(session)) == 1
    monkeypatch.setattr(game.settings, "telephone_enabled", False)
    await ui.start_creation(Message(bot, 1), state, 1, session)
    assert "non abilitato" in bot.sent[-1][1]


@pytest.mark.parametrize(
    "status,expected",
    [
        ("member", True),
        ("administrator", True),
        ("creator", True),
        ("restricted", True),
        ("left", False),
    ],
)
async def test_membership_is_fail_closed(setup, monkeypatch, status, expected):
    bot, _, _ = setup
    bot.member = status
    assert await ui._member(bot, 10) is expected
    bot.failure = "member"
    assert not await ui._member(bot, 10)
    monkeypatch.setattr(ui.group_registry, "get_group_id", lambda: 0)
    assert not await ui._member(bot, 10)


async def test_join_does_not_hijack_other_flows_and_never_sends_to_outsiders(session, setup):
    bot, state, generator = setup
    game_id = await running(session)
    await ui.join(Message(bot, chat_type="group"), state, session, game_id)
    assert not bot.sent
    state.get_state.return_value = "other-flow"
    await ui.join(Message(bot), state, session, game_id)
    assert "altra operazione" in bot.sent[-1][1]
    state.get_state.return_value = None
    bot.member = "left"
    await ui.join(Message(bot), state, session, game_id)
    assert "verificare" in bot.sent[-1][1]
    generator.assert_not_awaited()


async def test_double_join_resumes_without_extending_or_delivering_twice(session, setup):
    bot, state, _ = setup
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    before = await game.get_view(session, game_id)
    await ui.join(Message(bot), state, session, game_id)
    after = await game.get_view(session, game_id)
    assert before.token == after.token and before.since == after.since
    assert "tempo non riparte" in bot.sent[-1][1]


async def test_invalid_late_duplicate_and_unknown_replies_never_generate(session, setup):
    bot, state, generator = setup
    with pytest.raises(SkipHandler):
        await ui.reply(Message(bot, reply_id=999), session)
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    prompt = await prompt_id(session, game_id, 10)
    await ui.reply(Message(bot, text="x" * 151, reply_id=prompt), session)
    assert "150 caratteri" in bot.sent[-1][1]
    bot.member = "left"
    await ui.reply(Message(bot, text="scene", reply_id=prompt), session)
    assert "nessuna immagine" in bot.sent[-1][1]
    bot.member = "member"
    await game.finish(session, game_id, "admin_closed")
    await session.commit()
    await ui.reply(Message(bot, text="scene", reply_id=prompt), session)
    assert "nessuna nuova immagine" in bot.sent[-1][1]
    generator.assert_not_awaited()


@pytest.mark.parametrize("failure", ["provider", "photo"])
async def test_failed_paid_generation_closes_instead_of_retrying(session, setup, failure):
    bot, state, generator = setup
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    prompt = await prompt_id(session, game_id, 10)
    if failure == "provider":
        generator.side_effect = RuntimeError("safe test error")
    else:
        bot.failure = "photo"
    await ui.reply(Message(bot, text="scene", reply_id=prompt), session)
    assert (await game.get_view(session, game_id)).reason == "image_error"
    assert generator.await_count == 1
    assert any("senza ritentare" in text for _, text, _ in bot.sent)


async def test_delivery_failure_releases_only_claim_and_retains_seen_visit(session, setup):
    bot, state, _ = setup
    game_id = await running(session)
    c, _ = await game.claim(session, game_id, 99, "prev", GROUP)
    await game.delivered(session, c, 999)
    await game.begin_generation(session, c, "first")
    await game.complete(session, c, "previous-image")
    await session.commit()
    bot.failure = "photo"
    await ui.join(Message(bot), state, session, game_id)
    assert (await game.get_view(session, game_id)).token is None
    assert (await game.claim(session, game_id, 10, "again", GROUP))[1] == "seen"


async def test_delivery_expired_during_send_is_reported(session, setup, monkeypatch):
    bot, state, _ = setup
    game_id = await running(session)
    monkeypatch.setattr(game, "delivered", AsyncMock(return_value=False))
    await ui.join(Message(bot), state, session, game_id)
    assert any("durante la consegna" in t for _, t, _ in bot.sent)


async def test_publisher_noops_and_errors_are_bounded(session, setup):
    bot, _, _ = setup
    await ui.publish_card(bot, session, -1)
    ready = await game.create(session, 1)
    await session.commit()
    await ui.publish_card(bot, session, ready)
    assert not bot.sent
    game_id = await running(session)
    await ui.publish_card(bot, session, game_id)
    bot.failure = "same"
    await ui.publish_card(bot, session, game_id)
    bot.failure = "edit"
    with pytest.raises(TelegramBadRequest):
        await ui.publish_card(bot, session, game_id)
    await ui._refresh(bot, session, game_id)  # durable state is not rolled back
    bot.failure = None
    await game.finish(session, game_id, "admin_closed")
    await session.commit()
    await ui.publish_card(bot, session, game_id)
    assert "senza immagini" in bot.edits[-1]["text"]


async def test_partial_reveal_recovers_when_initial_card_was_not_sent(session, setup):
    bot, _, _ = setup
    game_id = await running(session)
    c, _ = await game.claim(session, game_id, 10, "user", GROUP)
    await game.delivered(session, c, 1)
    await game.begin_generation(session, c, "a scene")
    await game.complete(session, c, "file-1")
    await game.finish(session, game_id, "image_error")
    await session.commit()
    await ui.publish_card(bot, session, game_id)
    assert "catena raccolta" in bot.edits[-1]["media"].caption
    assert (await game.get_view(session, game_id)).anchor is not None


async def test_idempotent_edit_still_emits_one_pending_final_notification(session, setup):
    bot, _, _ = setup
    game_id = await running(session)
    await ui.publish_card(bot, session, game_id)
    await game.finish(session, game_id, "admin_closed")
    await session.commit()
    bot.failure = "same"
    await ui.publish_card(bot, session, game_id)
    await ui.publish_card(bot, session, game_id)
    assert sum(chat == GROUP for chat, _, _ in bot.sent) == 2
    assert (await game.get_view(session, game_id)).announced


def retry_callback(bot, game_id, *, user=10, action="retry"):
    return SimpleNamespace(
        bot=bot, message=Message(bot, user), from_user=SimpleNamespace(id=user), answer=AsyncMock()
    ), TelephoneCb(action=action, game_id=game_id)


async def test_transient_timeout_is_paused_and_one_manual_retry_can_succeed(session, setup):
    bot, state, generator = setup
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    prompt = await prompt_id(session, game_id, 10)
    generator.side_effect = ui.image_service.ImageError(
        "safe", kind="timeout", stage="generation", retryable=True
    )
    await ui.reply(Message(bot, text="scene", reply_id=prompt), session)
    view = await game.get_view(session, game_id)
    assert view.status == "running" and view.kind == "retry" and view.attempts == 1
    assert generator.await_count == 1
    assert any("staffetta è salva" in t for _, t, _ in bot.sent)
    await ui.join(Message(bot), state, session, game_id)
    assert bot.sent[-1][2]["reply_markup"] is not None
    wrong, data = retry_callback(bot, game_id, user=999)
    await ui.retry_control(wrong, data, state, session)
    assert generator.await_count == 1
    generator.side_effect = None
    cb, data = retry_callback(bot, game_id)
    await ui.retry_control(cb, data, state, session)
    assert generator.await_count == 2
    assert (await game.get_view(session, game_id)).stage == 2
    await ui.retry_control(cb, data, state, session)
    assert generator.await_count == 2


@pytest.mark.parametrize("action", ["retry", "leave"])
async def test_second_transient_failure_or_abandon_is_bounded(session, setup, action):
    bot, state, generator = setup
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    generator.side_effect = ui.image_service.ImageError(
        "safe", kind="http", http_status=503, retryable=True
    )
    await ui.reply(
        Message(bot, text="scene", reply_id=await prompt_id(session, game_id, 10)), session
    )
    cb, data = retry_callback(bot, game_id, action=action)
    await ui.retry_control(cb, data, state, session)
    view = await game.get_view(session, game_id)
    assert view.status == ("finished" if action == "retry" else "running")
    assert view.token is None
    assert generator.await_count == (2 if action == "retry" else 1)


async def test_retry_does_not_bypass_membership_or_other_fsm(session, setup):
    bot, state, generator = setup
    cb, data = retry_callback(bot, 999)
    state.get_state.return_value = "other"
    await ui.retry_control(cb, data, state, session)
    assert cb.answer.call_args.kwargs["show_alert"]
    state.get_state.return_value = None
    bot.member = "left"
    await ui.retry_control(cb, data, state, session)
    assert "Accesso" in cb.answer.call_args.args[0]
    generator.assert_not_awaited()


async def test_budget_failure_has_clear_message_and_no_retry_button(session, setup):
    bot, state, generator = setup
    game_id = await running(session)
    await ui.join(Message(bot), state, session, game_id)
    generator.side_effect = ui.ai_budget.AIBudgetExceeded("test")
    await ui.reply(
        Message(bot, text="scene", reply_id=await prompt_id(session, game_id, 10)), session
    )
    assert any("budget immagini" in t for _, t, _ in bot.sent)
    assert (await game.get_view(session, game_id)).status == "finished"
