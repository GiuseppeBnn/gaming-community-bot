"""Unit tests for utils.text — HTML escaping and message chunking."""

from __future__ import annotations

from utils.text import TELEGRAM_MESSAGE_LIMIT, chunk_blocks, chunk_html_lines, esc


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


def test_chunk_blocks_counts_emoji_as_two_utf16_units():
    blocks = ["🎮" * 1000, "🎮" * 1000, "fine"]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 2
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks)
    assert "\n".join(chunks) == "\n".join(blocks)


def test_chunk_blocks_counts_utf16_in_separator():
    assert chunk_blocks(["ab", "cd"], sep="🎮", limit=5) == ["ab", "cd"]


def test_html_chunks_preserve_every_row_including_emoji_and_escaped_text():
    rows = [f'{i}. <b>🎮 &lt;utente&gt;</b> — {i}' for i in range(200)]
    pages = chunk_html_lines("\n".join(rows))
    assert len(pages) > 1
    assert "\n".join(pages) == "\n".join(rows)
    assert all(len(page.encode("utf-16-le")) // 2 <= 3900 for page in pages)
    assert all(page.count("<b>") == page.count("</b>") for page in pages)


def test_html_chunks_never_split_multiline_titles_or_mentions():
    title = "<b>Titolo\nsu due righe</b>"
    name = '<a href="tg://user?id=1">Mario\nRossi</a>'
    text = title + "\n" + name + "\nfine"
    pages = chunk_html_lines(text, limit=50)
    assert "\n".join(pages) == text
    assert title in pages[0]
    assert name in pages[1]
    assert all(page.count("<a ") == page.count("</a>") for page in pages)


def test_html_chunks_keep_unclosed_final_block_intact():
    assert chunk_html_lines("<b>titolo\nnon chiuso") == ["<b>titolo\nnon chiuso"]


def test_html_chunks_do_not_treat_escaped_tags_or_self_closing_tags_as_open():
    assert chunk_html_lines("&lt;b&gt;\n<tag/>\nfine", limit=10) == ["&lt;b&gt;", "<tag/>", "fine"]
