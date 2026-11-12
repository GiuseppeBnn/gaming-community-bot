"""Offline Telegram doubles shared by native quiz tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.methods import SendPoll
from aiogram.types import PollAnswer, User

from services import quiz_service


class Bot:
    id = 777

    def __init__(self, session=None):
        self.session = session
        self.polls = []
        self.messages = []
        self.stopped = []
        self.send_error = None
        self.stop_error = None

    def _network(self):
        if self.session is not None:
            assert not self.session.in_transaction(), "SQL transaction held over Telegram"

    async def send_poll(self, **kwargs):
        self._network()
        if self.send_error:
            raise self.send_error
        SendPoll(**kwargs)  # exercise the installed aiogram request schema too
        self.polls.append(kwargs)
        index = len(self.polls)
        return SimpleNamespace(
            message_id=index,
            poll=SimpleNamespace(id=f"native-{id(self)}-{index}"),
            date=datetime.now(timezone.utc).replace(microsecond=0),
        )

    async def send_message(self, chat_id, text, **kwargs):
        self._network()
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=1000 + len(self.messages))

    async def stop_poll(self, chat_id, message_id):
        self._network()
        self.stopped.append((chat_id, message_id))
        if self.stop_error:
            raise self.stop_error

    @property
    def said(self):
        return "\n".join(text for _, text, _ in self.messages)


class Message:
    def __init__(self, bot=None, user_id=42, chat_type="private"):
        self.bot = bot or Bot()
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.chat = SimpleNamespace(id=user_id, type=chat_type)

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)


class Callback:
    def __init__(self, message, user_id=42):
        self.message = message
        self.bot = message.bot
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def answer(poll_id, option_ids=(0,), user_id=42):
    return PollAnswer(
        poll_id=poll_id,
        user=User(id=user_id, is_bot=False, first_name="Player"),
        option_ids=list(option_ids),
        option_persistent_ids=[str(option) for option in option_ids],
    )


async def make_quiz(session, *, status="running", count=2, limit=0, shuffle=False):
    quiz = await quiz_service.create_quiz(
        session,
        1,
        "Trivia <nerd>",
        "Una descrizione",
        randomize_questions=shuffle,
        randomize_answers=shuffle,
    )
    for index in range(count):
        await quiz_service.add_question(
            session,
            quiz.id,
            f"Domanda {index}?",
            ["A" * 100, "B" * 65, "C"],
            index % 3,
            "Perché è così.",
            time_limit_seconds=limit,
        )
    await quiz_service.set_status(session, quiz.id, status)
    await session.commit()
    return await quiz_service.get_quiz(session, quiz.id)
