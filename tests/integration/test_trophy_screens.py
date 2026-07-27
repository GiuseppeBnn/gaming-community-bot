"""The trophy screens — `handlers/badges.py`, at 53%.

Two screens that look similar and must not be: `/trofei` is **personal** (what you
have unlocked) and `/catalogo_trofei` is **public** (what exists). The difference
that matters is the secret trophies: masked in the catalog for everyone, revealed
in the personal screen once earned. Getting that backwards would either spoil every
hidden trophy or hide one from the person who owns it.

The rest is layout with teeth: entries are grouped by rarity under a section
header, and the whole thing is split into Telegram-sized messages on block
boundaries — a header separated from its section, or a message over 4096 chars,
is a screen the user simply cannot read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import Badge
from handlers import badges
from services import badge_service
from utils.text import esc

USER_ID = 7


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMessage:
    def __init__(self, *, chat_type: str = "private", user_id: int = USER_ID) -> None:
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=user_id, username="tizio", full_name="Tizio")
        self.chat = SimpleNamespace(
            id=user_id if chat_type == "private" else -100_123, type=chat_type
        )
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


async def _a_hidden_badge(session) -> Badge:
    badge = (await session.execute(
        select(Badge).where(Badge.hidden.is_(True)).limit(1)
    )).scalar_one_or_none()
    if badge is None:
        pytest.skip("the catalog has no hidden trophy")
    return badge


async def _any_badge(session) -> Badge:
    return (await session.execute(select(Badge).limit(1))).scalar_one()


class TestPersonalScreen:
    async def test_in_the_group_it_only_hands_back_a_link(self, session):
        """Your trophy list is a picture of what you play and how much; it belongs
        in private like every other personal screen (§9)."""
        message = _FakeMessage(chat_type="supergroup")

        await badges.cmd_traguardi(message, session)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=trofei")

    async def test_in_private_the_command_renders_the_screen(
        self, seeded_session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio")
        message = _FakeMessage()

        await badges.cmd_traguardi(message, seeded_session)

        assert "I tuoi Trofei" in message.said

    async def test_with_an_empty_catalog_it_says_so(self, session, user_factory):
        await user_factory(tg_id=USER_ID, username="tizio")
        message = _FakeMessage()

        await badges.show_traguardi(message, session)

        assert "Nessun trofeo disponibile" in message.said

    async def test_a_user_with_nothing_yet_is_told_where_to_look(
        self, seeded_session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio")
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        assert "catalogo_trofei" in message.said
        assert "0/" in message.said

    async def test_an_earned_trophy_is_listed_and_counted(
        self, seeded_session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio")
        badge = await _any_badge(seeded_session)
        await badge_service.award_badge(seeded_session, USER_ID, badge.slug)
        await seeded_session.commit()
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        # Names go through `esc`, and several contain an apostrophe.
        assert esc(badge.name) in message.said
        assert "(1/" in message.said

    async def test_a_secret_trophy_is_revealed_once_it_is_yours(
        self, seeded_session, user_factory
    ):
        """This is the payoff of a hidden trophy: it stays «???» to everyone until
        the person who unlocked it looks at their own list."""
        await user_factory(tg_id=USER_ID, username="tizio")
        hidden = await _a_hidden_badge(seeded_session)
        await badge_service.award_badge(seeded_session, USER_ID, hidden.slug)
        await seeded_session.commit()
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        assert esc(hidden.name) in message.said
        assert "???" not in message.said

    async def test_the_header_carries_level_and_active_tag(
        self, seeded_session, user_factory
    ):
        await user_factory(tg_id=USER_ID, username="tizio", xp=5000,
                           cosmetic_tag="👑 Reietto")
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        assert "Livello" in message.said and "👑 Reietto" in message.said

    async def test_an_unregistered_caller_still_gets_a_screen(self, seeded_session):
        """`/trofei` is reachable before the User row exists (a deep-link from the
        group); level 1 and an empty list beats a crash."""
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        assert "Livello 1" in message.said

    async def test_a_trophy_deleted_from_the_catalog_is_not_counted(
        self, seeded_session, user_factory
    ):
        """The count and the list are computed from the same filtered set, so a
        pruned trophy can't make them disagree («2/50» with one line shown)."""
        await user_factory(tg_id=USER_ID, username="tizio")
        badge = await _any_badge(seeded_session)
        await badge_service.award_badge(seeded_session, USER_ID, badge.slug)
        await seeded_session.commit()
        await seeded_session.delete(badge)
        await seeded_session.commit()
        message = _FakeMessage()

        await badges.show_traguardi(message, seeded_session)

        assert "(0/" in message.said


class TestPublicCatalog:
    async def test_an_empty_catalog_says_so(self, session):
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, session)

        assert "Catalogo trofei vuoto" in message.said

    async def test_the_catalog_lists_every_visible_trophy(self, seeded_session):
        all_badges = await badge_service.get_all_badges(seeded_session)
        visible = [b for b in all_badges if not b.hidden]
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, seeded_session)

        assert visible, "the seeded catalog must not be empty"
        missing = [b.name for b in visible if esc(b.name) not in message.said]
        assert missing == []

    async def test_secret_trophies_stay_masked_for_everyone(self, seeded_session):
        """The catalog is identical for every caller, so a hidden entry must not be
        revealed here even to someone who owns it."""
        hidden = await _a_hidden_badge(seeded_session)
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, seeded_session)

        assert esc(hidden.name) not in message.said
        assert "???" in message.said

    async def test_todays_catalog_fits_in_one_message(self, seeded_session):
        """Recorded as a fact, not as a requirement: the shipped catalog is ~3.5k
        characters. If a future trophy pushes it over, this test fails and points at
        the chunking test below rather than at a mystery API error in production."""
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, seeded_session)

        assert len(message.texts) == 1
        assert len(message.texts[0]) <= 4096

    async def test_a_catalog_too_big_for_one_message_is_split_into_valid_ones(
        self, seeded_session
    ):
        """The real boundary: with enough trophies a single message would exceed
        Telegram's 4096-char cap and be rejected outright."""
        for i in range(80):
            seeded_session.add(Badge(
                slug=f"riempitivo-{i}", name=f"Riempitivo {i}",
                description="d" * 200, icon_emoji="🧱", category="test",
                rarity="bronze", xp_reward=0, hidden=False,
            ))
        await seeded_session.commit()
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, seeded_session)

        assert len(message.texts) > 1
        assert all(0 < len(t) <= 4096 for t in message.texts)

    async def test_a_section_header_is_never_stranded_from_its_section(
        self, seeded_session
    ):
        """The separator and the header are glued into one block on purpose: a chunk
        break between them would open a message with a bare rule."""
        message = _FakeMessage()

        await badges.cmd_catalogo_trofei(message, seeded_session)

        assert not any(t.startswith(badges._TIER_SEP) and t.count("\n") == 0
                       for t in message.texts)


class TestTrophyRendering:
    async def test_an_unearned_trophy_shows_a_lock_and_its_goal(self, seeded_session):
        badge = (await seeded_session.execute(
            select(Badge).where(Badge.hidden.is_(False)).limit(1)
        )).scalar_one()

        block = badges._trophy_block(badge, earned=False)

        assert block.startswith("🔒") and esc(badge.name) in block

    async def test_an_earned_one_shows_a_tick(self, seeded_session):
        badge = await _any_badge(seeded_session)

        assert badges._trophy_block(badge, earned=True).startswith("✅")

    async def test_a_trophy_with_no_description_falls_back_to_its_condition(self):
        """An entry without a goal tells the user nothing about how to get it."""
        badge = SimpleNamespace(
            hidden=False, name="Senza testo", description="  ",
            condition_type="balance", condition_value=1000, condition_param=None,
        )

        block = badges._trophy_block(badge, earned=False)

        assert "1" in block and "—" in block

    async def test_a_trophy_with_neither_still_renders(self):
        badge = SimpleNamespace(
            hidden=False, name="Nudo", description=None,
            condition_type=None, condition_value=None, condition_param=None,
        )

        assert "Nudo" in badges._trophy_block(badge, earned=False)
