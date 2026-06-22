"""Unit tests for utils.text — HTML escaping and message chunking."""

from __future__ import annotations

from utils.text import TELEGRAM_MESSAGE_LIMIT, chunk_blocks, esc


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


# --- chunk_blocks ----------------------------------------------------------


def test_chunk_blocks_short_input_single_chunk():
    blocks = ["a", "b", "c"]
    assert chunk_blocks(blocks, sep="\n") == ["a\nb\nc"]


def test_chunk_blocks_empty_input():
    assert chunk_blocks([]) == []


def test_chunk_blocks_splits_when_over_limit():
    # 5 blocks of 30 chars, limit 100 → packs ~3 per chunk on "\n" joins.
    blocks = ["x" * 30 for _ in range(5)]
    chunks = chunk_blocks(blocks, sep="\n", limit=100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100
    # No block is lost or split.
    rejoined = "\n".join(chunks)
    assert rejoined.replace("\n", "") == "x" * 150


def test_chunk_blocks_never_splits_a_block():
    blocks = ["short", "y" * 80, "short"]
    chunks = chunk_blocks(blocks, sep="\n", limit=50)
    # The 80-char block exceeds the limit but is emitted whole, not cut.
    assert any(block == "y" * 80 for chunk in chunks for block in chunk.split("\n"))


def test_chunk_blocks_respects_separator_length():
    chunks = chunk_blocks(["aaaa", "bbbb", "cccc"], sep="\n\n", limit=10)
    for chunk in chunks:
        assert len(chunk) <= 10


def test_chunk_blocks_default_limit_under_telegram_max():
    # A realistic over-limit catalogue stays within the hard Telegram ceiling.
    blocks = [f"<b>Trofeo {i}</b> — descrizione di esempio" for i in range(200)]
    for chunk in chunk_blocks(blocks, sep="\n\n"):
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT
