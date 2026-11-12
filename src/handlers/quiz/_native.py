"""The single native-poll presenter shared by real play and admin dry-runs."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InputPollOption

log = logging.getLogger(__name__)


async def send_question(
    bot: Bot,
    chat_id: int,
    question,
    ordered_options: list[tuple[int, str]],
    *,
    index: int,
    total: int,
    time_limit: int = 0,
    trial: bool = False,
    reply_markup=None,
):
    """Never clip answers or prepend progress to the 300-character question."""
    # Old quizzes allowed more line breaks than native explanations support.
    # Preserve their text without rewriting DB content or truncating answers.
    explanation = question.explanation or None
    if explanation is not None and explanation.count("\n") > 2:
        explanation = " ".join(explanation.splitlines())
    return await bot.send_poll(
        chat_id=chat_id,
        question=question.text,
        question_parse_mode=None,
        description=f"{'🧪 PROVA · ' if trial else ''}Domanda {index + 1}/{total}",
        description_parse_mode=None,
        options=[InputPollOption(text=text, text_parse_mode=None) for _, text in ordered_options],
        type="quiz",
        is_anonymous=False,
        allows_multiple_answers=False,
        allows_revoting=False,
        correct_option_ids=[
            next(
                index
                for index, (real_id, _) in enumerate(ordered_options)
                if real_id == question.correct_option_id
            )
        ],
        explanation=explanation,
        explanation_parse_mode=None,
        open_period=time_limit or None,
        protect_content=True,
        reply_markup=reply_markup,
    )


async def stop_question(bot: Bot, chat_id: int, message_id: int) -> None:
    """Cosmetic closure must never prevent a committed answer from advancing."""
    try:
        await bot.stop_poll(chat_id=chat_id, message_id=message_id)
    except Exception as exc:  # best effort after the scoring transaction commits
        log.warning("Quiz poll closure failed error=%s", type(exc).__name__)
