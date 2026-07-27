"""`handlers/_targeting.resolve_target` — who an admin command acts on.

Every admin command that names a user goes through this one function: `/ban`,
`/addebita`, `/setsaldo`, `/mute`, `/warn`. It decides **who gets banned** and, via
`remainder`, **how many coins move**, out of whatever the admin typed in a group.

Four ways to name someone, tried in a fixed order, and the order is the design: in a
busy group replying to the offending message is the fastest and least ambiguous, so
it wins over anything typed in the arguments. The rest exist because Telegram does
not guarantee a username — a `text_mention` entity is the only way to point at
someone who has none.

`remainder` is the other half of the job and the easier one to get wrong: everything
after the target token, which the caller then parses as an amount. Strip too little
and `/addebita @mario 100` tries to debit «@mario 100»; strip too much and the amount
disappears.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.enums import MessageEntityType

from handlers._targeting import resolve_target


def _message(text: str = "", *, reply_from=None, entities=None):
    return SimpleNamespace(
        text=text,
        entities=entities or [],
        reply_to_message=SimpleNamespace(from_user=reply_from) if reply_from else None,
    )


def _tg_user(tg_id: int, *, username: str | None = None, full_name: str = "Tizio",
             is_bot: bool = False):
    return SimpleNamespace(id=tg_id, username=username, full_name=full_name, is_bot=is_bot)


def _entity(offset: int, length: int, user):
    return SimpleNamespace(
        type=MessageEntityType.TEXT_MENTION, offset=offset, length=length, user=user
    )


class TestByReply:
    async def test_replying_targets_the_author_and_keeps_all_args(
        self, session, user_factory
    ):
        """Replying is the primary path: the whole argument string stays as remainder,
        because none of it was spent naming anyone."""
        await user_factory(tg_id=7, username="mario")
        message = _message("/addebita 100 spam", reply_from=_tg_user(7, username="mario"))

        target = await resolve_target(message, session, "100 spam")

        assert target.tg_id == 7
        assert target.user is not None and target.user.username == "mario"
        assert target.remainder == "100 spam"
        assert target.display_name == "@mario"

    async def test_a_reply_wins_over_a_username_in_the_arguments(
        self, session, user_factory
    ):
        """Both are present and they disagree. The reply wins — and this is worth
        pinning precisely because the other choice is also defensible: an admin who
        replies to Mario and types «@luigi» has contradicted themselves, and the bot
        must resolve it the same way every time.
        """
        await user_factory(tg_id=7, username="mario")
        await user_factory(tg_id=8, username="luigi")
        message = _message("/ban @luigi", reply_from=_tg_user(7, username="mario"))

        target = await resolve_target(message, session, "@luigi")

        assert target.tg_id == 7
        assert target.remainder == "@luigi"

    async def test_replying_to_a_bot_does_not_target_it(self, session, user_factory):
        """Bots post the quiz panels and the announcements, so a reply to one is an
        admin replying to the bot's own message — not a request to ban the bot."""
        await user_factory(tg_id=8, username="luigi")
        message = _message("/ban @luigi", reply_from=_tg_user(999, is_bot=True))

        target = await resolve_target(message, session, "@luigi")

        assert target.tg_id == 8, "the reply to a bot should have been ignored"

    async def test_an_unregistered_author_still_resolves_by_id(self, session):
        """Moderation only needs the id; `user is None` is what currency commands
        check to refuse. Losing the id here would make it impossible to ban someone
        who never talked to the bot."""
        message = _message("/ban", reply_from=_tg_user(1234, full_name="Sconosciuto"))

        target = await resolve_target(message, session, "")

        assert target.tg_id == 1234
        assert target.user is None
        assert "Sconosciuto" in target.display_name


class TestByMention:
    async def test_a_text_mention_resolves_a_user_without_a_username(
        self, session, user_factory
    ):
        """The only way to point at someone with no @username. Telegram sends the id
        in the entity; the visible text is just their name."""
        await user_factory(tg_id=9, username=None, full_name="Senza Nome")
        text = "/addebita Senza Nome 50"
        offset = text.index("Senza Nome")
        message = _message(
            text, entities=[_entity(offset, len("Senza Nome"), _tg_user(9, full_name="Senza Nome"))]
        )

        target = await resolve_target(message, session, "Senza Nome 50")

        assert target.tg_id == 9
        assert target.remainder == "50", "the mention text was not stripped from the args"

    async def test_a_mention_of_an_unregistered_user_keeps_the_id(self, session):
        text = "/ban Tizio Ignoto"
        offset = text.index("Tizio Ignoto")
        message = _message(
            text,
            entities=[_entity(offset, len("Tizio Ignoto"), _tg_user(555, full_name="Tizio Ignoto"))],
        )

        target = await resolve_target(message, session, "Tizio Ignoto")

        assert target.tg_id == 555 and target.user is None

    async def test_entities_that_are_not_mentions_are_ignored(
        self, session, user_factory
    ):
        """A message can carry bold, links, code — none of them name a user."""
        await user_factory(tg_id=8, username="luigi")
        bold = SimpleNamespace(type=MessageEntityType.BOLD, offset=0, length=4, user=None)
        message = _message("/ban @luigi", entities=[bold])

        target = await resolve_target(message, session, "@luigi")

        assert target.tg_id == 8


class TestByUsernameOrId:
    async def test_a_username_resolves_and_leaves_the_rest(self, session, user_factory):
        await user_factory(tg_id=7, username="mario")
        message = _message("/addebita @mario 100 motivo lungo")

        target = await resolve_target(message, session, "@mario 100 motivo lungo")

        assert target.tg_id == 7
        assert target.remainder == "100 motivo lungo"

    async def test_an_unknown_username_yields_no_id(self, session):
        """Nothing to act on, but the shape is still returned so the caller can say
        «utente non trovato» instead of «specifica un utente»."""
        message = _message("/ban @fantasma")

        target = await resolve_target(message, session, "@fantasma")

        assert target.tg_id is None and target.user is None
        assert "fantasma" in target.display_name

    async def test_a_numeric_id_resolves(self, session, user_factory):
        await user_factory(tg_id=7, username="mario")
        message = _message("/addebita 7 100")

        target = await resolve_target(message, session, "7 100")

        assert target.tg_id == 7 and target.remainder == "100"

    async def test_a_negative_id_is_accepted(self, session):
        """Channel and anonymous-admin ids are negative. Rejecting the minus sign
        would make them unmoderatable."""
        message = _message("/ban -1001234567890")

        target = await resolve_target(message, session, "-1001234567890")

        assert target.tg_id == -1001234567890

    async def test_a_bare_word_is_not_a_target(self, session):
        """Neither a mention, nor an @username, nor a number — so the caller must ask
        again rather than guess. Treating it as a username would silently resolve to
        nobody and read as «utente non registrato»."""
        message = _message("/ban tizio")

        assert await resolve_target(message, session, "tizio") is None

    async def test_no_arguments_at_all_is_not_a_target(self, session):
        assert await resolve_target(_message("/ban"), session, "") is None
        assert await resolve_target(_message("/ban"), session, None) is None
        assert await resolve_target(_message("/ban"), session, "   ") is None


class TestDisplayName:
    async def test_the_username_is_preferred_over_the_full_name(
        self, session, user_factory
    ):
        await user_factory(tg_id=7, username="mario", full_name="Mario Rossi")
        target = await resolve_target(_message("/ban @mario"), session, "@mario")
        assert target.display_name == "@mario"

    async def test_html_in_a_name_is_escaped(self, session, user_factory):
        """`display_name` is interpolated into HTML replies, so it is escaped once
        here rather than at each of the dozen call sites. A user can set their own
        full name, which makes this the actual attack surface."""
        await user_factory(tg_id=7, username=None, full_name="<b>grassetto</b>")

        target = await resolve_target(_message("/ban 7"), session, "7")

        assert "<b>" not in target.display_name
        assert "&lt;b&gt;" in target.display_name

    async def test_an_unknown_numeric_target_falls_back_to_the_id(self, session):
        target = await resolve_target(_message("/ban 4242"), session, "4242")
        assert target.display_name == "ID 4242"
