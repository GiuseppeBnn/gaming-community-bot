"""Unit tests for utils.text.esc — HTML escaping of user-controlled strings."""

from __future__ import annotations

from utils.text import esc


def test_escapes_angle_brackets_and_ampersand():
    assert esc("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert esc("a & b") == "a &amp; b"


def test_escapes_quotes():
    assert esc('"q"') == "&quot;q&quot;"
    assert esc("it's") == "it&#x27;s"


def test_none_becomes_empty_string():
    assert esc(None) == ""


def test_non_string_is_coerced():
    assert esc(123) == "123"


def test_clean_text_is_unchanged():
    assert esc("Mario Rossi") == "Mario Rossi"


def test_truncation_adds_ellipsis():
    # len 6 > limit 4 → first 3 chars + ellipsis
    assert esc("abcdef", limit=4) == "abc…"


def test_no_truncation_within_limit():
    assert esc("abc", limit=10) == "abc"


def test_truncation_happens_before_escaping():
    # "<<<<<"[:1] + "…" = "<…" then escaped
    assert esc("<<<<<", limit=2) == "&lt;…"


def test_injection_payload_is_neutralised():
    payload = '<a href="tg://user?id=1">x</a>'
    out = esc(payload)
    assert "<a" not in out
    assert "&lt;a" in out
