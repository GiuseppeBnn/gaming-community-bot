"""Native timers replace in-memory countdowns; validate payload boundaries."""

from __future__ import annotations

from types import SimpleNamespace

from handlers.quiz import _native
from handlers.quiz._shared import _explanation_error, _options_error, _too_long
from tests.quiz_support import Bot


async def test_full_length_question_and_options_are_never_clipped():
    bot = Bot()
    question = SimpleNamespace(text="Q" * 300, correct_option_id=1, explanation="<b>raw</b>")
    await _native.send_question(
        bot,
        42,
        question,
        [(1, "🐧" * 50), (0, "A" * 100)],
        index=9,
        total=10,
    )
    sent = bot.polls[0]
    assert len(sent["question"]) == 300 and sent["description"] == "Domanda 10/10"
    assert sent["correct_option_ids"] == [0]
    assert sent["options"][0].text == "🐧" * 50
    assert all(option.text_parse_mode is None for option in sent["options"])
    assert sent["explanation"] == "<b>raw</b>" and sent["explanation_parse_mode"] is None


def test_poll_limits_count_emoji_like_telegram():
    assert _options_error(["🐧" * 50, "B"]) is None
    assert "102/100" in _options_error(["🐧" * 51, "B"])
    assert _too_long("🐧" * 150, 300, "Domanda") is None
    assert "302/300" in _too_long("🐧" * 151, 300, "Domanda")
    assert _explanation_error("a\nb\nc") is None
    assert "due a capo" in _explanation_error("a\nb\nc\nd")


async def test_legacy_multiline_explanation_keeps_all_text_without_rewriting_it():
    bot = Bot()
    question = SimpleNamespace(text="Q", correct_option_id=0, explanation="a\nb\nc\nd")
    await _native.send_question(bot, 42, question, [(0, "A"), (1, "B")], index=0, total=1)
    assert bot.polls[0]["explanation"] == "a b c d"
    assert question.explanation == "a\nb\nc\nd"


def test_admin_correct_selector_displays_full_escaped_options_above_buttons():
    from handlers.quiz._shared import _correct_prompt

    assert _correct_prompt(["A" * 100, "<Luigi> & Mario"]) == (
        "Quale opzione è quella <b>corretta</b>?\n\n<b>1.</b> " + "A" * 100
        + "\n<b>2.</b> &lt;Luigi&gt; &amp; Mario"
    )
