"""Tests for the quiz free-text length limits.

Over-long input is REJECTED, never silently truncated: an admin who only finds
out after the quiz is live can no longer fix it without editing every question.
"""

from __future__ import annotations

from handlers.quiz import (
    _MAX_EXPLANATION,
    _MAX_OPTION,
    _MAX_QUESTION,
    _MAX_TITLE,
    _MAX_OPTIONS,
    _MIN_OPTIONS,
    _options_error,
    _too_long,
)


class TestTooLong:
    def test_accepts_text_at_the_cap(self):
        assert _too_long("x" * 10, 10, "Il titolo è troppo lungo") is None

    def test_rejects_one_over_the_cap(self):
        err = _too_long("x" * 11, 10, "Il titolo è troppo lungo")
        assert err is not None
        assert "11/10" in err

    def test_reports_how_much_to_cut(self):
        err = _too_long("x" * 350, _MAX_QUESTION, "La domanda è troppo lunga")
        assert f"Accorcia di {350 - _MAX_QUESTION}" in err

    def test_empty_text_is_fine(self):
        assert _too_long("", _MAX_EXPLANATION, "La spiegazione è troppo lunga") is None


class TestOptionsError:
    def test_accepts_a_valid_list(self):
        assert _options_error(["Roma", "Milano"]) is None

    def test_rejects_too_few(self):
        err = _options_error(["Roma"])
        assert err is not None and str(_MIN_OPTIONS) in err

    def test_rejects_too_many(self):
        err = _options_error([f"o{i}" for i in range(_MAX_OPTIONS + 1)])
        assert err is not None and str(_MAX_OPTIONS) in err

    def test_rejects_an_over_long_option_and_names_it(self):
        err = _options_error(["ok", "x" * (_MAX_OPTION + 5)])
        assert err is not None
        assert "opzione 2" in err                      # points at the offending line
        assert f"{_MAX_OPTION + 5}/{_MAX_OPTION}" in err

    def test_option_exactly_at_the_cap_is_accepted(self):
        assert _options_error(["ok", "x" * _MAX_OPTION]) is None


class TestCapsAreConsistent:
    def test_caps_are_positive(self):
        for cap in (_MAX_TITLE, _MAX_QUESTION, _MAX_OPTION, _MAX_EXPLANATION):
            assert cap > 0

    def test_service_truncation_backstop_matches_the_handler_caps(self):
        """The service still truncates as a last-resort backstop; if those numbers
        ever drift from the handler caps, the rejection message would lie."""
        import inspect

        import services.quiz_service as qz

        src = inspect.getsource(qz.create_quiz) + inspect.getsource(qz.add_question)
        assert f"[:{_MAX_TITLE}]" in src
        assert f"[:{_MAX_QUESTION}]" in src
        assert f"[:{_MAX_EXPLANATION}]" in src
