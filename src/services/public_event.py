"""Immutable public event projection shared by discovery and event types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PublicEvent:
    key: str
    item_id: int
    title: str
    summary: str
    emoji: str
    deep_link_payload: str | None = None
    starts_at: datetime | None = None
    schedule_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.starts_at is None

    @property
    def result_id(self) -> str:
        if self.schedule_id is not None:
            return f"soon:{self.schedule_id}"
        return f"open:{self.key}:{self.item_id}"
