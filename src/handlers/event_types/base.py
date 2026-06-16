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

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask


@dataclass(frozen=True)
class StartResult:
    """Outcome of starting/closing an item, rendered back on the callback.

    ``message`` is shown via ``callback.answer``; ``alert`` raises it as a modal
    pop-up (used for errors).
    """

    ok: bool
    message: str
    alert: bool = False


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
    ) -> None:
        """Run a due scheduled task. Raises on failure; the loop commits."""
        ...

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        """Close/finish a running item (e.g. publish a quiz podium). ``None`` if
        the type has no close action."""
        ...


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
