"""describe_condition: plain-Italian unlock text shared by /catalogo_badge and
/traguardi (no dev-jargon 'type ≥ value')."""

from __future__ import annotations

import pytest

from services.badge_service import describe_condition


class TestDescribeCondition:
    def test_onboarding_ignores_value(self):
        assert describe_condition("onboarding", 1) == "Completa la registrazione"

    def test_balance_is_thousands_formatted(self):
        assert describe_condition("balance", 1000) == "Raggiungi 1,000 Aldueuri"

    def test_daily_streak(self):
        assert describe_condition("daily_streak", 7) == "Riscuoti il /daily per 7 giorni di fila"

    def test_bets_won(self):
        assert describe_condition("bets_won", 5) == "Vinci 5 scommesse"

    def test_transfers_made(self):
        assert describe_condition("transfers_made", 1) == "Invia 1 trasferimenti"

    def test_xp(self):
        assert describe_condition("xp", 2000) == "Accumula 2,000 XP"

    @pytest.mark.parametrize("ct", [None, "", "unknown_type"])
    def test_missing_or_unknown_type_is_none(self, ct):
        assert describe_condition(ct, 10) is None

    def test_none_value_does_not_crash(self):
        # A machine condition with no value still renders (treated as 0).
        assert describe_condition("xp", None) == "Accumula 0 XP"

    def test_all_catalog_condition_types_are_covered(self):
        # Every condition type the milestone engine understands must have wording.
        from services.catalog_loader import TROPHY_CONDITIONS

        for ct in TROPHY_CONDITIONS:
            assert describe_condition(ct, 1) is not None, ct
