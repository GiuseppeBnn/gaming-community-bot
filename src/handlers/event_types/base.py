"""
Event-type registry — the single extension point of the events subsystem.

Each event type (quiz · poll · bet · …) is described by an :class:`EventType`
spec and registered once via :func:`register`. The events hub
(``handlers.events``) and the scheduler executor (``handlers.schedule``) dispatch
**only** through this registry — never with per-type ``if/elif`` chains. To add a
new event type, implement a spec and register it in
:func:`handlers.event_types.register_builtin`; no hub/scheduler edits are required
(STEERING dev-rule on event types).

Commit convention (STEERING §5): spec methods **never commit**. The caller owns
the transaction — the callback handler commits after ``start_now`` / ``close_now``
returns ``ok``; the scheduler loop commits after ``execute_scheduled``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask


PostCommitHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StartResult:
    """Outcome of starting/closing an item, rendered back on the callback.

    ``message`` is shown via ``callback.answer``; ``alert`` raises it as a modal
    pop-up (used for errors).
    """

    ok: bool
    message: str
    alert: bool = False
    post_commit: PostCommitHook | None = None


@runtime_checkable
class EventType(Protocol):
    """Contract every event type implements. Methods never commit (see module doc)."""

    key: str           # task_type discriminator persisted on ScheduledTask
    hub_label: str     # e.g. "🧠 Quiz" — hub buttons, schedule menu, /programmati
    create_label: str  # e.g. "➕ Crea quiz"

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        """Render the admin list of pre-created items of this type (hub view)."""
        ...

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        """Return ``(id, label)`` of items that can be scheduled right now."""
        ...

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int
    ) -> None:
        """Enter the (admin) creation FSM for a new item of this type."""
        ...

    async def start_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult:
        """Start the item in the group now. Mutates but does not commit."""
        ...

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> PostCommitHook | None:
        """Run a due task and optionally defer presentation until the loop commits."""
        ...

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        """Close/finish a running item (e.g. publish a quiz podium). ``None`` if
        the type has no close action."""
        ...

    # ------------------------------------------------------------------
    # Optional capabilities. The events hub probes these with ``getattr`` and
    # falls back to the generic item screen for types that don't implement them,
    # so they are deliberately NOT part of the required contract above — adding
    # them here would break ``isinstance(et, EventType)`` for types that opt out.
    #
    #   async def render_detail(self, message, db_session, item_id) -> None
    #       Info/detail screen (title, status, stats, …) with status-aware action
    #       buttons that route through the ``ev:ask*`` confirmation steps.
    #
    #   async def delete(self, db_session, item_id) -> StartResult
    #       Permanently delete the item (mutates, never commits).
    #
    #   async def archive(self, db_session, item_id) -> StartResult
    #       Hide durable completed history (mutates, never commits).
    #
    #   async def reset(self, db_session, item_id) -> StartResult | None
    #       Re-arm a finished item so it can run again ("Riproponi"); ``None`` when
    #       the type is not re-runnable.
    #
    #   async def discover_open(self, db_session) -> list[PublicEvent]
    #       Public, currently playable cards for inline mode.
    #
    #   async def describe_scheduled(self, db_session, item_id) -> PublicEvent | None
    #       Resolve a future ScheduledTask without teaching inline mode about the
    #       concrete event model. Return None when the item is no longer startable.
    #
    # ------------------------------------------------------------------


_REGISTRY: dict[str, EventType] = {}


def register(event_type: EventType) -> None:
    """Register (or replace) an event type by its ``key``."""
    _REGISTRY[event_type.key] = event_type


def get(key: str) -> EventType | None:
    return _REGISTRY.get(key)


def all_types() -> list[EventType]:
    """Registered types in insertion order (drives hub/schedule menu order)."""
    return list(_REGISTRY.values())


def clear() -> None:
    """Test helper: empty the registry."""
    _REGISTRY.clear()


async def edit_or_send(
    message: Message, text: str, kb: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the message in place when possible, else send a fresh one.

    Shared by the hub and every type's ``render_list`` (callbacks edit, DMs send).
    """
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 — message may be too old / identical / not editable
        await message.answer(text, reply_markup=kb)
