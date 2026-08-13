"""Unit tests for the event-type registry (handlers/event_types).

The registry is a process-global dict, so every test snapshots and restores it to
avoid leaking registrations into the rest of the suite.
"""

from __future__ import annotations

import pytest

from handlers import event_types
from handlers.event_types.base import _REGISTRY, EventType, StartResult


@pytest.fixture(autouse=True)
def _isolate_registry():
    snapshot = dict(_REGISTRY)
    event_types.clear()
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class _Demo:
    """A minimal spec used to exercise the registry without touching the DB."""

    def __init__(self, key: str = "demo", label: str = "🎮 Demo") -> None:
        self.key = key
        self.hub_label = label
        self.create_label = f"➕ {label}"


def test_register_then_get_and_all():
    et = _Demo()
    event_types.register(et)
    assert event_types.get("demo") is et
    assert event_types.all_types() == [et]


def test_get_unknown_returns_none():
    assert event_types.get("nope") is None


def test_register_replaces_by_key():
    first, second = _Demo(label="A"), _Demo(label="B")
    event_types.register(first)
    event_types.register(second)
    assert event_types.get("demo") is second
    assert event_types.all_types() == [second]  # not duplicated


def test_all_types_preserves_insertion_order():
    for k in ("a", "b", "c"):
        event_types.register(_Demo(key=k))
    assert [t.key for t in event_types.all_types()] == ["a", "b", "c"]


def test_clear_empties_registry():
    event_types.register(_Demo())
    event_types.clear()
    assert event_types.all_types() == []


def test_register_builtin_registers_every_type_in_hub_order():
    """The order is the hub's button order, so it is behaviour and not a list.
    `guess` and `sound` are the same spec registered twice — one engine, two
    games — which is why they sit next to each other."""
    event_types.register_builtin()
    assert [t.key for t in event_types.all_types()] == [
        "quiz", "guess", "sound", "poll", "bet", "twentyq", "raid",
    ]


_REQUIRED_METHODS = (
    "render_list",
    "schedulable_items",
    "start_creation",
    "start_now",
    "execute_scheduled",
    "close_now",
)


@pytest.mark.parametrize("key", ["quiz", "poll", "bet"])
def test_builtin_specs_satisfy_contract(key):
    event_types.register_builtin()
    et = event_types.get(key)
    assert et.key == key
    assert isinstance(et.hub_label, str) and et.hub_label
    assert isinstance(et.create_label, str) and et.create_label
    for method in _REQUIRED_METHODS:
        assert callable(getattr(et, method)), f"{key}.{method} missing"
    # The runtime_checkable Protocol recognises a conformant spec.
    assert isinstance(et, EventType)


def test_start_result_defaults_to_no_alert():
    res = StartResult(True, "ok")
    assert (res.ok, res.message, res.alert) == (True, "ok", False)
