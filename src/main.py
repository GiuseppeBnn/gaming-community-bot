import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
)
from config_data.config import settings
from database.connection import async_session_maker, create_tables, engine, run_migrations
import handlers
from handlers import errors, event_types
from handlers.schedule import scheduler_loop
from middlewares.ban_guard import BannedUserMiddleware
from middlewares.db_middleware import DbSessionMiddleware
from middlewares.group_guard import GroupMemberMiddleware
from middlewares.group_context import GroupContextMiddleware
from middlewares.rate_limit import RateLimitMiddleware
from services import badge_service, catalog_loader, group_registry, igdb_catalog, twenty_questions_catalog
from services.backup.loop import backup_loop
from utils import alerts
from utils.atomic_io import probe_writable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

_PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Menu principale"),
    BotCommand(command="profilo", description="Il tuo profilo"),
    BotCommand(command="saldo", description="Saldo e ultimi movimenti"),
    BotCommand(command="storico", description="Cronologia completa"),
    BotCommand(command="daily", description="Premio giornaliero"),
    BotCommand(command="trasferisci", description="Trasferisci CoInn"),
    BotCommand(command="scommesse", description="Scommesse aperte"),
    BotCommand(command="crea_scommessa", description="Crea una scommessa"),
    BotCommand(command="quiz", description="🧠 Quiz: gioca o gestisci"),
    BotCommand(command="guessthegame", description="🖼️ Guess The Game attivi"),
    BotCommand(command="soundquest", description="🔊 Sound Quest attivi"),
    BotCommand(command="trofei", description="I tuoi trofei e rango"),
    BotCommand(command="catalogo_trofei", description="Tutti i trofei"),
    BotCommand(command="classifiche", description="Classifiche: ricchezza, XP, trofei"),
    BotCommand(command="locanda", description="🍺 Locanda: tag e consumabili"),
    BotCommand(command="d20", description="Tira un d20"),
    BotCommand(command="comandi", description="Guida ai comandi"),
    BotCommand(command="spiega_comando", description="Spiegazione di un comando"),
]

_GROUP_COMMANDS = [
    BotCommand(command="scommesse", description="Scommesse aperte"),
    BotCommand(command="crea_scommessa", description="Crea una scommessa"),
    BotCommand(command="daily", description="Premio giornaliero"),
    BotCommand(command="saldo", description="Il tuo saldo"),
    BotCommand(command="trasferisci", description="Trasferisci CoInn"),
    BotCommand(command="profilo", description="Il tuo profilo"),
    BotCommand(command="quiz", description="🧠 Quiz attivo da giocare"),
    BotCommand(command="guessthegame", description="🖼️ Guess The Game attivi"),
    BotCommand(command="soundquest", description="🔊 Sound Quest attivi"),
    BotCommand(command="trofei", description="I tuoi trofei e rango"),
    BotCommand(command="catalogo_trofei", description="Tutti i trofei"),
    BotCommand(command="classifiche", description="Classifiche della community"),
    BotCommand(command="locanda", description="🍺 Locanda: tag e consumabili"),
    BotCommand(command="d20", description="Tira un d20"),
    BotCommand(command="maestro", description="Trasforma uno sfogo in filosofia"),
    BotCommand(command="complotto", description="Teoria del complotto sul messaggio"),
    BotCommand(command="difendi", description="Avvocato difensore del messaggio"),
    BotCommand(command="accusa", description="Inquisitore: condanna il messaggio"),
    BotCommand(command="drama", description="Versione anime drammatica"),
    BotCommand(command="dialetto", description="Traduce in siciliano grezzo"),
    BotCommand(command="insulta", description="Blasta un utente taggato"),
    BotCommand(command="alduino", description="🐲 Parla con Alduino, il drago della community"),
    BotCommand(command="comandi", description="Guida ai comandi"),
]

# Admins additionally see their tools in the "/" menu. Shown only to admins via
# the per-chat / chat-administrators scopes (never in the public lists, §18).
_ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="admin", description="🔧 Pannello admin"),
    BotCommand(command="gestisci_scommesse", description="🔧 Gestione scommesse"),
    BotCommand(command="eventi", description="🎬 Hub eventi"),
    BotCommand(command="crea_quiz", description="🎬 Crea un quiz"),
    BotCommand(command="sondaggio", description="🎬 Crea un sondaggio"),
    BotCommand(command="programma", description="🎬 Programma un evento"),
    BotCommand(command="programmati", description="🎬 Eventi programmati"),
    BotCommand(command="info", description="📊 Dossier utente"),
    BotCommand(command="cerca", description="📊 Cerca utenti"),
    BotCommand(command="stats", description="📊 Statistiche community"),
    BotCommand(command="audit", description="📊 Registro azioni admin"),
    BotCommand(command="lista_ranghi", description="📊 Sistema ranghi e livelli"),
    BotCommand(command="airdrop", description="💰 Airdrop CoInn"),
    BotCommand(command="credita", description="💰 Accredita CoInn"),
    BotCommand(command="addebita", description="💰 Addebita CoInn"),
    BotCommand(command="warn", description="🛡️ Ammonisci un utente"),
    BotCommand(command="ban", description="🛡️ Banna un utente"),
    BotCommand(command="mute", description="🛡️ Silenzia un utente"),
]
_ADMIN_COMMANDS = _PRIVATE_COMMANDS + _ADMIN_EXTRA_COMMANDS


async def _build_storage() -> BaseStorage:
    """Scegli lo storage FSM, degradando a memoria se Redis non risponde.

    `RedisStorage.from_url` builds a client without connecting, so a broken or
    absent Redis used to sail through startup and surface much later, one
    amnesiac conversation at a time. The ping moves that discovery here and
    turns it into a degradation instead of a bot that looks alive and cannot
    remember anything.

    Degrading rather than refusing to start is the trade STEERING §2 asks for:
    losing the open FSM flows is worth less than losing the bot.
    """
    if settings.fsm_storage != "redis":
        return MemoryStorage()

    try:
        from aiogram.fsm.storage.redis import RedisStorage
    except ImportError:
        logger.warning("redis non installato, uso MemoryStorage")
        return MemoryStorage()

    storage = RedisStorage.from_url(settings.redis_url)
    try:
        await storage.redis.ping()
    except Exception as exc:  # noqa: BLE001 — any connection failure degrades
        logger.warning(
            "Redis non raggiungibile su %s (%s): uso MemoryStorage. "
            "Le conversazioni FSM aperte non sopravvivranno a un riavvio.",
            settings.redis_url, exc,
        )
        # The half-built client holds a connection pool; closing it must not be
        # able to replace a degradation with a crash.
        with contextlib.suppress(Exception):
            await storage.close()
        return MemoryStorage()

    return storage


async def main() -> None:
    alerts.install()
    await create_tables()
    await run_migrations()
    logger.info("Tabelle DB pronte.")

    # Early visibility on a mis-mounted/non-writable backup volume: warn loudly at
    # boot (the backup loop also re-checks each tick) but never block startup.
    _backup_probe = probe_writable(settings.backup_dir)
    if _backup_probe is not None:
        logger.warning(
            "Cartella backup «%s» non scrivibile (%s): i backup verranno saltati. "
            "Verifica il volume montato su /app/%s.",
            settings.backup_dir, _backup_probe, settings.backup_dir,
        )

    # Load the customizable CSV catalogs (trophies → DB, ranks/cosmetics → memory),
    # falling back to built-in defaults if the files are absent/invalid.
    async with async_session_maker() as session:
        n_trophies = await badge_service.sync_trophies(session)
        # Restore the effective group id (may differ from settings.group_id after
        # a chat migration) before any handler runs.
        effective_group = await group_registry.load(session)
    counts = catalog_loader.init_registries()
    counts["twenty_questions"] = twenty_questions_catalog.init_catalog()
    logger.info(
        "Cataloghi caricati: %d trofei, %d ranghi, %d cosmetici, %d consumabili "
        "(%d categorie), %d giochi per 20 Domande. Group id effettivo: %s",
        n_trophies, counts["ranks"], counts["cosmetics"], counts["consumables"],
        counts["consumable_categories"], counts["twenty_questions"], effective_group,
    )

    # Populate the event-type registry before any event handler / the scheduler
    # loop runs. New event types plug in here — the hub and scheduler dispatch
    # only through this registry (no per-type if/elif).
    event_types.register_builtin()

    storage = await _build_storage()
    # The settings value is what was *requested*; after a degrade it no longer
    # matches what got built (STEERING §2) — log the class that is actually wired in.
    logger.info("FSM storage: %s", type(storage).__name__)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Middleware order matters: rate-limit first, then DB session, then the
    # bot-level ban guard (needs db_session; silently drops banned users), then
    # the group-membership guard and finally the best-effort context recorder.
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(BannedUserMiddleware())
    dp.update.middleware(GroupMemberMiddleware())
    dp.update.middleware(GroupContextMiddleware())

    # Order is behaviour, not bookkeeping (aiogram stops at the first match), so it
    # is declared once in handlers/__init__.py and asserted by
    # tests/unit/test_router_order.py — including that nothing is left unregistered.
    handlers.register(dp)

    # Global fallback for anything that escapes a handler: logs with context and
    # replies to the user instead of leaving the bot silent. On the dispatcher
    # (not a router) so it covers every handler registered above.
    dp.errors.register(errors.on_error)

    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

    # Admin tools in the "/" menu, scoped so only admins see them:
    #  • each configured super-admin's private chat (best-effort: needs them to
    #    have started the bot, else Telegram errors → we just skip),
    #  • the configured group's administrators.
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                _ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:  # noqa: BLE001 — chat may not exist yet; non-fatal
            logger.warning("Comandi admin non registrati per %s (chat non avviata?)", admin_id)
    group_id = group_registry.get_group_id()
    if group_id:
        try:
            await bot.set_my_commands(
                _ADMIN_COMMANDS, scope=BotCommandScopeChatAdministrators(chat_id=group_id)
            )
        except Exception:  # noqa: BLE001
            logger.warning("Comandi admin di gruppo non registrati (group_id=%s)", group_id)
    logger.info("Comandi bot registrati.")

    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    backup_task = asyncio.create_task(backup_loop())
    catalog_task = asyncio.create_task(igdb_catalog.catalog_sync_loop())
    alert_task = asyncio.create_task(alerts.alert_loop(bot))

    logger.info("Bot avviato — polling in corso.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        background_tasks = (scheduler_task, backup_task, catalog_task, alert_task)
        for task in background_tasks:
            task.cancel()
        # Cancelled *before* the farewell drain, not after: the loop's own tick
        # runs every _POLL_INTERVAL_SECONDS, so draining alongside a live task
        # means two coroutines sharing the dedup state, and a suppressed-repeat
        # count can go unreported. Cancel first, then drain once, uncontested.
        done, pending = await asyncio.wait(background_tasks, timeout=5)
        for task in done:
            if not task.cancelled() and (exc := task.exception()) is not None:
                logger.error("Task background terminato con errore durante lo shutdown: %s", exc)
        if pending:
            logger.warning(
                "%d task background non hanno terminato entro 5 secondi.", len(pending)
            )
        # A clean SIGTERM (Watchtower restarts the container every
        # WATCHTOWER_POLL_INTERVAL) must not throw away up to _MAX_BUFFERED alerts
        # that never got a poll tick to go out.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(alerts.drain(bot), 5)
        # Close every long-lived resource explicitly.  In particular, relying on
        # a driver's worker thread being daemonized made clean process exit depend
        # on the exact SQLAlchemy/aiosqlite version pair.
        for resource_name, close in (
            ("sessione Telegram", bot.session.close),
            ("storage FSM", storage.close),
            ("engine database", engine.dispose),
        ):
            try:
                await close()
            except Exception:  # noqa: BLE001 — shutdown continues, but stays observable
                logger.exception("Errore chiudendo %s.", resource_name)
        logger.info("Bot fermato.")


if __name__ == "__main__":
    asyncio.run(main())
