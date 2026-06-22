"""Unit tests for the trophy-screen renderers in handlers.badges.

The screens must mirror note.txt: one line per trophy as ``<status> <name> —
<description>`` — no arbitrary per-trophy emoji, no repeated unlock line, no
per-trophy rarity label (the tier lives in the section header). Secret trophies
stay masked until earned.
"""

from __future__ import annotations

from dataclasses import dataclass

from handlers import badges
from services.badge_service import RARITY_HEADERS


@dataclass
class _Badge:
    id: int
    name: str
    description: str
    rarity: str
    hidden: bool = False
    condition_type: str | None = None
    condition_value: int | None = None
    condition_param: str | None = None


class TestTrophyBlock:
    def test_locked_shows_name_and_description_only(self):
        b = _Badge(1, "Spiccioli", "Accumula 1.000 CoInn nel saldo.", "bronze")
        assert badges._trophy_block(b, earned=False) == (
            "🔒 <b>Spiccioli</b> — Accumula 1.000 CoInn nel saldo."
        )

    def test_earned_uses_check_marker_and_bold(self):
        b = _Badge(1, "Spiccioli", "Accumula 1.000 CoInn nel saldo.", "bronze")
        assert badges._trophy_block(b, earned=True) == (
            "✅ <b>Spiccioli</b> — Accumula 1.000 CoInn nel saldo."
        )

    def test_no_per_trophy_rarity_label_or_unlock_line(self):
        # The old format repeated the rarity and a "Come sbloccarlo:" line; neither
        # should survive (only one description, next to the name).
        b = _Badge(1, "Gran Puntatore", "Vinci 100 scommesse totali.", "gold")
        out = badges._trophy_block(b, earned=False)
        assert "Come sbloccarlo" not in out
        assert "Oro" not in out and "·" not in out
        assert out.count("—") == 1

    def test_hidden_not_earned_is_masked(self):
        b = _Badge(1, "Re degli Ultimi", "Arriva ultimo 100 volte.", "gold", hidden=True)
        assert badges._trophy_block(b, earned=False) == "🔒 <i>??? — Trofeo nascosto</i>"

    def test_hidden_earned_is_revealed(self):
        b = _Badge(1, "Re degli Ultimi", "Arriva ultimo 100 volte.", "gold", hidden=True)
        assert badges._trophy_block(b, earned=True) == (
            "✅ <b>Re degli Ultimi</b> — Arriva ultimo 100 volte."
        )

    def test_blank_description_falls_back_to_condition(self):
        # A CSV-authored trophy with no description still shows its goal.
        b = _Badge(1, "Senza testo", "", "bronze",
                   condition_type="balance", condition_value=1000)
        out = badges._trophy_block(b, earned=False)
        assert out == "🔒 <b>Senza testo</b> — Raggiungi 1,000 CoInn"

    def test_blank_description_no_condition_omits_dash(self):
        b = _Badge(1, "Manuale", "", "bronze")
        assert badges._trophy_block(b, earned=False) == "🔒 <b>Manuale</b>"

    def test_name_and_description_are_html_escaped(self):
        b = _Badge(1, "A<b>", "x & <y>", "bronze")
        out = badges._trophy_block(b, earned=True)
        assert "<b>A&lt;b&gt;</b>" in out
        assert "x &amp; &lt;y&gt;" in out


class TestGroupedTrophyBlocks:
    def _badges(self):
        return [
            _Badge(1, "Bronzo1", "d", "bronze"),
            _Badge(2, "Oro1", "d", "gold"),
            _Badge(3, "Platino1", "d", "platinum"),
            _Badge(4, "Argento1", "d", "silver"),
            _Badge(5, "Bronzo2", "d", "bronze"),
        ]

    def test_grouped_platinum_first_then_descending(self):
        blocks = badges._grouped_trophy_blocks(self._badges(), set())
        # First block is the platinum header (no separator), tiers descend.
        assert blocks[0] == RARITY_HEADERS["platinum"]
        headers = [b for b in blocks if "Trofei" in b or "Trofeo" in b]
        order = [h.split("\n")[-1] for h in headers]
        assert order == [
            RARITY_HEADERS["platinum"], RARITY_HEADERS["gold"],
            RARITY_HEADERS["silver"], RARITY_HEADERS["bronze"],
        ]

    def test_separator_between_tiers_not_before_first(self):
        blocks = badges._grouped_trophy_blocks(self._badges(), set())
        assert not blocks[0].startswith(badges._TIER_SEP)
        # Every later header carries the separator glued above it.
        later_headers = [b for b in blocks[1:] if b.endswith(("Oro", "Argento", "Bronzo"))]
        assert later_headers and all(h.startswith(badges._TIER_SEP) for h in later_headers)

    def test_earned_ids_drive_the_marker(self):
        blocks = badges._grouped_trophy_blocks(self._badges(), {2})
        entries = [b for b in blocks if "—" in b or b.startswith(("✅", "🔒"))]
        earned = [b for b in entries if b.startswith("✅")]
        assert len(earned) == 1 and "Oro1" in earned[0]
