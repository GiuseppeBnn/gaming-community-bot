"""Il canale di alert deve essere impossibile da trasformare in un guasto.

Tre modi in cui un canale del genere si autodistrugge, e i tre test che li
chiudono: `emit` che fa I/O e blocca l'event loop dentro un handler; il sender
che logga i propri errori e si rialimenta all'infinito; una singola riga di log
in loop che riempie la chat e fa smettere di guardarla.
"""

from __future__ import annotations

import asyncio
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


def test_a_same_prefixed_logger_name_is_not_mistaken_for_our_own():
    """A bare `.startswith(__name__)` would also swallow `utils.alerts_v2` — a
    logger that only shares the prefix, not our own submodule. stdlib's own
    `logging.Filter` guards exactly this case with a dot-boundary check."""
    handler = alerts.TelegramAlertHandler()

    handler.emit(_record(name="utils.alerts_v2"))

    assert len(alerts._buffer) == 1


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


def test_fingerprint_distinguishes_exception_types_on_the_same_template():
    """`handlers.errors` and `aiogram.event` log *every* exception under one
    template. Without the exception type in the fingerprint, an unrelated second
    bug sharing that template would be folded into the first one's dedup window
    — its traceback would never be sent, ever."""
    import sys

    def _record_for(exc_type: type[Exception]) -> logging.LogRecord:
        try:
            raise exc_type("boom")
        except exc_type:
            return _record(
                msg="Unhandled error", args=(), name="aiogram.event", exc_info=sys.exc_info()
            )

    value_error = _record_for(ValueError)
    type_error = _record_for(TypeError)
    another_value_error = _record_for(ValueError)

    assert alerts._fingerprint(value_error) != alerts._fingerprint(type_error)
    assert alerts._fingerprint(value_error) == alerts._fingerprint(another_value_error)


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


def test_format_keeps_the_suppressed_count_even_when_the_traceback_is_truncated():
    """The suppressed count used to be appended after the traceback and cut
    along with it — losing exactly the signal that tells «one hiccup» from «a
    storm» apart (see `_fingerprint`)."""
    import sys

    try:
        raise ValueError("x" * 6000)
    except ValueError:
        record = _record(msg="esploso", args=(), exc_info=sys.exc_info())

    text = alerts.format_alert(record, suppressed=42)

    assert "soppresse" in text


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


def test_a_real_log_call_reaches_the_buffer_and_the_threshold_filters(monkeypatch):
    """Every other test calls `handler.emit()` directly, which skips
    `Logger.callHandlers`' own level gate (`record.levelno >= handler.level`) —
    so the threshold from `_min_level()` was never actually exercised end to end."""
    monkeypatch.setattr(settings, "alert_min_level", "WARNING")
    handler = alerts.install()
    try:
        log = logging.getLogger("services.whatever")
        log.setLevel(logging.INFO)
        log.info("sotto soglia")
        log.warning("sopra soglia")
        assert [r.getMessage() for r in alerts._buffer] == ["sopra soglia"]
    finally:
        logging.getLogger().removeHandler(handler)


class _FakeBot:
    """Registra le consegne. `fails=True` è il caso che non deve mai loggare."""

    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.parse_modes: list = []
        self._fails = fails

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if self._fails:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text))
        self.parse_modes.append(parse_mode)


async def test_drain_delivers_to_every_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11, 22])
    alerts._buffer.append(_record())
    bot = _FakeBot()

    sent = await alerts.drain(bot)

    assert sent == 1
    assert [chat_id for chat_id, _ in bot.sent] == [11, 22]
    assert bot.parse_modes == [None, None], "un traceback non è HTML"


async def test_drain_empties_the_buffer(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._buffer.append(_record())
    bot = _FakeBot()

    await alerts.drain(bot)

    assert not alerts._buffer


async def test_a_delivery_failure_neither_raises_nor_logs(monkeypatch, caplog):
    """Se il sender loggasse, l'errore rientrerebbe nel buffer, per sempre."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._buffer.append(_record())
    bot = _FakeBot(fails=True)

    with caplog.at_level(logging.DEBUG):
        await alerts.drain(bot)

    assert alerts._undelivered == 1, "la consegna fallita si conta"
    ours = [r for r in caplog.records if r.name.startswith("utils.alerts")]
    assert not ours, "…e non si logga: rientrerebbe nel buffer, per sempre"


async def test_repeats_are_delivered_once(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    for _ in range(5):
        alerts._buffer.append(_record())
    bot = _FakeBot()

    sent = await alerts.drain(bot)

    assert sent == 1
    assert len(bot.sent) == 1


async def test_housekeeping_reports_what_was_lost(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._dropped = 7
    alerts._undelivered = 3
    bot = _FakeBot()

    await alerts.drain(bot)

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "7" in text and "3" in text
    assert alerts._dropped == 0 and alerts._undelivered == 0


async def test_housekeeping_does_not_report_itself_forever(monkeypatch):
    """I contatori si azzerano **prima** dell'invio: una notifica di consegna
    fallita che fallisce a sua volta non deve ripresentarsi a ogni giro."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._dropped = 2
    bot = _FakeBot(fails=True)

    await alerts.drain(bot)
    await alerts.drain(bot)

    assert alerts._undelivered == 1, "il fallimento non deve sparire nel reset di se' stesso"


async def test_housekeeping_is_rate_limited(monkeypatch):
    """Col canale a terra, ogni tick riproverebbe: una chiamata API ogni 2s, per
    sempre. La notifica di servizio passa dallo stesso dedup di tutto il resto."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    bot = _FakeBot()

    alerts._dropped = 2
    await alerts.drain(bot)
    alerts._dropped = 3
    await alerts.drain(bot)

    assert len(bot.sent) == 1, "la seconda cade nella finestra di dedup"
    assert alerts._dropped == 3, "…e il conteggio resta in attesa, non si perde"


async def test_drain_on_an_empty_buffer_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    bot = _FakeBot()

    assert await alerts.drain(bot) == 0
    assert not bot.sent


async def test_alert_loop_survives_a_failing_drain_without_logging(monkeypatch, caplog):
    """Mirrors `test_the_loop_survives_a_failing_tick` in test_backup_loop.py.
    `alert_loop`'s contract is stricter than the backup loop's: a logged failure
    here would feed straight back into the buffer it exists to drain, so a raising
    `drain` must be swallowed silently, not just survived."""

    async def boom(bot):
        raise RuntimeError("drain exploded")

    class Stop(Exception):
        pass

    async def fake_sleep(_seconds):
        raise Stop

    monkeypatch.setattr(alerts, "drain", boom)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Stop):
            await alerts.alert_loop(object())

    ours = [r for r in caplog.records if r.name.startswith("utils.alerts")]
    assert not ours, "a logged failure would feed back into the buffer it drains"
