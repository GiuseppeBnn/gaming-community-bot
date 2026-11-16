"""Trivia Nerd naming is public API; persisted quiz identities are not renamed."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, User

from filters import admin_filter
from handlers import help_content, quiz
from handlers.callbacks import EventCb
from handlers.event_types.quiz_type import QuizType
from handlers.quiz import creation, lifecycle
from main import _ADMIN_EXTRA_COMMANDS, _GROUP_COMMANDS, _PRIVATE_COMMANDS
from services import quiz_service


@pytest.mark.parametrize("command,target,admin,allowed", [
    ("trivianerd", lifecycle.cmd_quiz_list, False, True),
    ("trivianerd", lifecycle.cmd_quiz_list, True, True),
    ("quiz", lifecycle.cmd_quiz_list, True, False),
    ("crea_trivianerd", creation.cmd_crea_quiz, True, True),
    ("crea_trivianerd", creation.cmd_crea_quiz, False, False),
    ("crea_quiz", creation.cmd_crea_quiz, True, False),
    ("avvia_trivianerd", lifecycle.cmd_avvia_quiz, True, True),
    ("avvia_trivianerd", lifecycle.cmd_avvia_quiz, False, False),
    ("avvia_quiz", lifecycle.cmd_avvia_quiz, True, False),
    ("chiudi_trivianerd", lifecycle.cmd_chiudi_quiz, True, True),
    ("chiudi_trivianerd", lifecycle.cmd_chiudi_quiz, False, False),
    ("chiudi_quiz", lifecycle.cmd_chiudi_quiz, True, False),
])
async def test_canonical_commands_route_without_weakening_admin_filters(monkeypatch, command, target, admin, allowed):
    monkeypatch.setattr(admin_filter, "is_admin", AsyncMock(return_value=admin))
    bot = Bot(token="1234567890:AAFtesttoken_ci")
    message = Message(
        message_id=1, date=datetime(2026, 11, 16), text=f"/{command}",
        chat=Chat(id=7, type="private"), from_user=User(id=7, first_name="Player", is_bot=False),
    ).as_(bot)
    registered = next(h for h in quiz.router.message.handlers if h.callback is target)
    try:
        matched, _ = await registered.check(message, bot=bot)
        assert matched is allowed
    finally:
        await bot.session.close()


def test_menus_manual_and_ai_reference_use_canonical_names_without_duplicates():
    old = {"quiz", "crea_quiz", "avvia_quiz", "chiudi_quiz"}
    for menu in (_PRIVATE_COMMANDS, _GROUP_COMMANDS):
        names = [entry.command for entry in menu]
        assert names.count("trivianerd") == 1
        assert not old.intersection(names)
        assert not any(name.endswith("_trivianerd") for name in names)
    admin_names = [entry.command for entry in _ADMIN_EXTRA_COMMANDS]
    for name in ("crea_trivianerd", "avvia_trivianerd", "chiudi_trivianerd"):
        assert admin_names.count(name) == 1
        assert help_content.render_command(name) is None
        assert help_content.render_command(name, is_admin=True) is not None
    assert not old.intersection(admin_names)
    assert sum(doc.name == "trivianerd" for doc in help_content._COMMANDS) == 1
    public = help_content.render_command("trivianerd")
    assert public is not None and "Trivia Nerd" in public
    assert "/trivianerd" in help_content.render_legend()
    reference = help_content.render_alduino_reference()
    assert "/trivianerd" in reference
    assert "/crea_trivianerd" not in reference
    for name in old:
        assert help_content.lookup(name) is None


async def test_existing_quiz_rows_are_discovered_with_new_links_and_unchanged_keys(session, user_factory):
    await user_factory(1)
    running = await quiz_service.create_quiz(session, 1, "Già in corso", "Descrizione")
    await quiz_service.add_question(session, running.id, "Domanda?", ["Sì", "No"], 0, None)
    await quiz_service.set_status(session, running.id, "running")
    ready = await quiz_service.create_quiz(session, 1, "Già programmabile", "Descrizione")
    await quiz_service.add_question(session, ready.id, "Domanda?", ["Sì", "No"], 0, None)
    await quiz_service.set_status(session, ready.id, "ready")
    await session.commit()
    spec = QuizType()
    assert spec.key == "quiz" and spec.hub_label == "🧠 Trivia Nerd"
    events = await spec.discover_open(session)
    assert [(e.key, e.item_id, e.deep_link_payload) for e in events] == [
        ("quiz", running.id, f"trivianerd_{running.id}"),
    ]
    scheduled = await spec.describe_scheduled(session, ready.id)
    assert scheduled is not None and scheduled.deep_link_payload == f"trivianerd_{ready.id}"
    assert EventCb(action="new", task_type=spec.key).pack() == "ev:new:quiz:"


def test_current_operational_docs_do_not_teach_removed_commands():
    root = Path(__file__).resolve().parents[2]
    removed = re.compile(r"(?<![\w./])/(?:quiz|crea_quiz|avvia_quiz|chiudi_quiz)(?![\w./])")
    for name in ("README.md", "STEERING.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert removed.search(text) is None, name
        for command in ("trivianerd", "crea_trivianerd", "avvia_trivianerd", "chiudi_trivianerd"):
            assert f"/{command}" in text
