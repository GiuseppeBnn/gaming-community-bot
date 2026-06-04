"""Integration tests for admin XP management (parity with audit log)."""

from __future__ import annotations

import pytest

from services import admin_service, xp_service
from services.xp_service import XpSource

pytestmark = pytest.mark.asyncio


class TestAdminGrantXp:
    async def test_grant_updates_xp_and_writes_audit(self, session, user_factory):
        await user_factory(tg_id=1, xp=0)

        # Mirrors handlers/admin.cmd_dai_xp + dashboard xpgrant.
        res = await xp_service.grant_xp(session, 1, 250, XpSource.admin_grant, capped=False)
        await admin_service.log_action(session, 999, "xp_grant", target_tg_id=1, amount=250)
        await session.commit()

        assert res.granted == 250
        actions = await admin_service.recent_actions(session, target_tg_id=1)
        assert any(a.action_type == "xp_grant" and a.amount == 250 for a in actions)

    async def test_admin_grant_is_uncapped(self, session, user_factory, monkeypatch):
        from config_data.config import settings
        monkeypatch.setattr(settings, "xp_daily_participation_cap", 10)
        await user_factory(tg_id=2, xp=0)
        res = await xp_service.grant_xp(session, 2, 5000, XpSource.admin_grant, capped=False)
        assert res.granted == 5000  # admin events ignore the participation cap

    async def test_airdrop_xp_writes_audit(self, session, user_factory):
        await user_factory(tg_id=3, xp=0)
        await user_factory(tg_id=4, xp=0)
        count = await xp_service.airdrop_xp(session, 100)
        await admin_service.log_action(session, 999, "xp_airdrop", amount=100, detail=f"{count} utenti")
        await session.commit()

        assert count == 2
        actions = await admin_service.recent_actions(session)
        assert any(a.action_type == "xp_airdrop" and a.amount == 100 for a in actions)


class TestXpLeaderboard:
    async def test_orders_by_xp_desc(self, session, user_factory):
        await user_factory(tg_id=10, xp=50)
        await user_factory(tg_id=11, xp=500)
        await user_factory(tg_id=12, xp=200)
        rows = await xp_service.leaderboard_xp(session)
        assert [u.tg_id for u, _ in rows] == [11, 12, 10]
