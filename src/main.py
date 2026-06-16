import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
)

from config_data.config import settings
from database.connection import async_session_maker, create_tables, run_migrations
from handlers import (
    admin,
    admin_betting,
    admin_dashboard,
    backup,
    badges,
    betting,
    common,
    economy,
    event_types,
    events,
    fun_ai,
    group_events,
    leaderboard,
    onboarding,
    quiz,
    schedule,
    shop,
)
from handlers.schedule import scheduler_loop
from middlewares.ban_guard import BannedUserMiddleware
from middlewares.db_middleware import DbSessionMiddleware
from middlewares.group_guard import GroupMemberMiddleware
from middlewares.rate_limit import RateLimitMiddleware
from services import badge_service, catalog_loader, group_registry
from services.backup.loop import backup_loop
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
    BotCommand(command="traguardi", description="I tuoi trofei e rango"),
    BotCommand(command="catalogo_badge", description="Tutti i trofei"),
    BotCommand(command="classifiche", description="Classifiche: ricchezza, XP, trofei"),
    BotCommand(command="negozio", description="Personalizzazioni (tag)"),
    BotCommand(command="comandi", description="Guida ai comandi"),
    BotCommand(command="spiega_comando", description="Spiegazione di un comando"),
]

_GROUP_COMMANDS = [
    BotCommand(command="scommesse", description="Scommesse aperte"),
    BotCommand(command="crea_scommessa", description="Crea una scommessa"),
    BotCommand(command="daily", description="Premio giornaliero"),
    BotCommand(command="saldo", description="Il tuo saldo"),
    BotCommand(command="profilo", description="Il tuo profilo"),
    BotCommand(command="quiz", description="🧠 Quiz attivo da giocare"),
    BotCommand(command="traguardi", description="I tuoi trofei e rango"),
    BotCommand(command="classifiche", description="Classifiche della community"),
    BotCommand(command="negozio", description="Personalizzazioni (tag)"),
    BotCommand(command="maestro", description="Trasforma uno sfogo in filosofia"),
    BotCommand(command="complotto", description="Teoria del complotto sul messaggio"),
    BotCommand(command="difendi", description="Avvocato difensore del messaggio"),
    BotCommand(command="accusa", description="Inquisitore: condanna il messaggio"),
    BotCommand(command="drama", description="Versione anime drammatica"),
    BotCommand(command="dialetto", description="Traduce in siciliano grezzo"),
    BotCommand(command="insulta", description="Blasta un utente taggato"),
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
    BotCommand(command="airdrop", description="💰 Airdrop CoInn"),
    BotCommand(command="credita", description="💰 Accredita CoInn"),
    BotCommand(command="addebita", description="💰 Addebita CoInn"),
    BotCommand(command="warn", description="🛡️ Ammonisci un utente"),
    BotCommand(command="ban", description="🛡️ Banna un utente"),
    BotCommand(command="mute", description="🛡️ Silenzia un utente"),
]
_ADMIN_COMMANDS = _PRIVATE_COMMANDS + _ADMIN_EXTRA_COMMANDS


def _build_storage():
    if settings.fsm_storage == "redis":
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            return RedisStorage.from_url(settings.redis_url)
        except ImportError:
            logger.warning("redis non installato, uso MemoryStorage")
    return MemoryStorage()


async def main() -> None:
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
    logger.info(
        "Cataloghi caricati: %d trofei, %d ranghi, %d cosmetici. Group id effettivo: %s",
        n_trophies, counts["ranks"], counts["cosmetics"], effective_group,
    )

    # Populate the event-type registry before any event handler / the scheduler
    # loop runs. New event types plug in here — the hub and scheduler dispatch
    # only through this registry (no per-type if/elif).
    event_types.register_builtin()

    storage = _build_storage()
    logger.info("FSM storage: %s", settings.fsm_storage)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Middleware order matters: rate-limit first, then DB session, then the
    # bot-level ban guard (needs db_session; silently drops banned users), then
    # the group-membership guard.
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(BannedUserMiddleware())
    dp.update.middleware(GroupMemberMiddleware())

    # Router order matters: admin_betting MUST precede betting so that
    # admin_bet:* callbacks are matched before the catch-all deny at the
    # bottom of admin_betting.router.
    dp.include_router(group_events.router)
    dp.include_router(onboarding.router)
    dp.include_router(economy.router)
    dp.include_router(admin_betting.router)
    dp.include_router(betting.router)
    dp.include_router(badges.router)
    dp.include_router(leaderboard.router)
    dp.include_router(shop.router)
    dp.include_router(admin.router)
    dp.include_router(admin_dashboard.router)
    dp.include_router(events.router)
    dp.include_router(quiz.router)
    dp.include_router(schedule.router)
    dp.include_router(backup.router)
    dp.include_router(fun_ai.router)
    dp.include_router(common.router)

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

    logger.info("Bot avviato — polling in corso.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler_task.cancel()
        backup_task.cancel()
        await bot.session.close()
        logger.info("Bot fermato.")


if __name__ == "__main__":
    asyncio.run(main())
