"""Il canale di alert deve essere impossibile da trasformare in un guasto.

Tre modi in cui un canale del genere si autodistrugge, e i tre test che li
chiudono: `emit` che fa I/O e blocca l'event loop dentro un handler; il sender
che logga i propri errori e si rialimenta all'infinito; una singola riga di log
in loop che riempie la chat e fa smettere di guardarla.
"""

from __future__ import annotations

import logging

import pytest

from config_data.config import settings
from utils import alerts


@pytest.fixture(autouse=True)
def clean_alerts():
    alerts.reset()
    yield
    alerts.reset()


def _record(
    msg: str = "Annuncio round %s fallito",
    *,
    level: int = logging.WARNING,
    name: str = "handlers.guess.lifecycle",
    args: tuple = (7,),
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=exc_info,
    )


def test_emit_only_buffers():
    """Se `emit` facesse I/O bloccherebbe l'event loop a ogni riga di log."""
    handler = alerts.TelegramAlertHandler()

    handler.emit(_record())

    assert len(alerts._buffer) == 1


def test_buffer_is_bounded_and_counts_what_it_drops():
    handler = alerts.TelegramAlertHandler()

    for _ in range(alerts._MAX_BUFFERED + 5):
        handler.emit(_record())

    assert len(alerts._buffer) == alerts._MAX_BUFFERED
    assert alerts._dropped == 5, "gli scartati si contano, non spariscono"


def test_own_records_are_ignored():
    """Un guasto di questo modulo che diventasse un alert si rialimenterebbe."""
    handler = alerts.TelegramAlertHandler()

    handler.emit(_record(name=alerts.__name__))

    assert not alerts._buffer


def test_repeats_inside_the_window_are_suppressed_and_counted():
    now = 1000.0
    fingerprint = ("handlers.guess.lifecycle", "Annuncio round %s fallito")

    first, suppressed = alerts._should_send(fingerprint, now)
    second, _ = alerts._should_send(fingerprint, now + 1)
    third, _ = alerts._should_send(fingerprint, now + 2)

    assert first is True and suppressed == 0
    assert second is False and third is False

    after, suppressed_after = alerts._should_send(
        fingerprint, now + alerts._DEDUP_WINDOW_SECONDS + 1
    )
    assert after is True
    assert suppressed_after == 2, "le ripetizioni si riportano, non si buttano"


def test_different_templates_are_not_deduplicated():
    now = 1000.0

    first, _ = alerts._should_send(("a", "template uno"), now)
    second, _ = alerts._should_send(("a", "template due"), now)

    assert first is True and second is True


def test_fingerprint_groups_by_template_not_by_formatted_message():
    """«round 7» e «round 8» sono lo stesso guasto, non due."""
    assert alerts._fingerprint(_record(args=(7,))) == alerts._fingerprint(_record(args=(8,)))


def test_format_carries_level_logger_and_message():
    text = alerts.format_alert(_record())

    assert "WARNING" in text
    assert "handlers.guess.lifecycle" in text
    assert "Annuncio round 7 fallito" in text, "il messaggio va formattato con i suoi args"


def test_format_includes_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _record(msg="esploso", args=(), exc_info=sys.exc_info())

    text = alerts.format_alert(record)

    assert "ValueError: boom" in text
    assert "Traceback" in text


def test_format_reports_suppressed_repeats():
    text = alerts.format_alert(_record(), suppressed=12)

    assert "12" in text


def test_format_truncates_instead_of_letting_telegram_refuse():
    text = alerts.format_alert(_record(msg="x" * 10_000, args=()))

    assert len(text) <= alerts._MAX_TEXT + 20
    assert "troncato" in text


def test_level_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "ERROR")

    assert alerts._min_level() == logging.ERROR


def test_a_nonsense_level_falls_back_to_warning(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "URGENTISSIMO")

    assert alerts._min_level() == logging.WARNING


def test_install_attaches_to_the_root_logger(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "ERROR")
    handler = alerts.install()
    try:
        assert handler in logging.getLogger().handlers
        assert handler.level == logging.ERROR
    finally:
        logging.getLogger().removeHandler(handler)
