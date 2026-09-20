from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

from middlewares import group_context as middleware


class Session:
    committed = False

    async def commit(self):
        self.committed = True


class Factory:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return None


def _message(*, text="ciao", chat_id=-100, chat_type="supergroup", bot=False):
    return SimpleNamespace(
        message_id=8,
        text=text,
        caption=None,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(
            id=7, is_bot=bot, full_name="Mario", first_name="M", username="mario",
        ),
        reply_to_message=SimpleNamespace(message_id=4),
    )


async def test_valid_group_message_is_captured_before_handler(monkeypatch):
    seen = {}
    session = Session()

    async def record(db_session, **kwargs):
        seen.update(session=db_session, **kwargs)

    async def handler(event, data):
        assert session.committed is True
        return "handled"

    monkeypatch.setattr(middleware.group_registry, "get_group_id", lambda: -100)
    monkeypatch.setattr(middleware, "async_session_maker", lambda: Factory(session))
    monkeypatch.setattr(middleware.group_context, "record_message", record)
    event = SimpleNamespace(message=_message(), edited_message=None)
    result = await middleware.GroupContextMiddleware()(handler, event, {})

    assert result == "handled"
    assert seen["group_id"] == -100 and seen["reply_to_message_id"] == 4
    assert seen["display_name"] == "Mario"


async def test_commands_bots_private_and_other_groups_are_ignored(monkeypatch):
    calls = 0

    async def record(*args, **kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(middleware.group_context, "record_message", record)
    monkeypatch.setattr(middleware.group_registry, "get_group_id", lambda: -100)
    instance = middleware.GroupContextMiddleware()
    for message in (
        _message(text="/alduino ciao"),
        _message(bot=True),
        _message(chat_type="private"),
        _message(chat_id=-200),
        _message(text=""),
    ):
        await instance._capture(message)
    assert calls == 0


async def test_storage_failure_and_disabled_capture_never_block_update(monkeypatch):
    async def broken(*args, **kwargs):
        raise SQLAlchemyError("db")

    async def handler(event, data):
        return 42

    monkeypatch.setattr(middleware.group_registry, "get_group_id", lambda: 0)
    monkeypatch.setattr(middleware, "async_session_maker", lambda: Factory(Session()))
    monkeypatch.setattr(middleware.group_context, "record_message", broken)
    instance = middleware.GroupContextMiddleware()
    assert await instance(handler, SimpleNamespace(message=_message()), {}) == 42

    monkeypatch.setattr(middleware.settings, "alduino_capture_group_context", False)
    assert await instance(handler, SimpleNamespace(message=_message()), {}) == 42
