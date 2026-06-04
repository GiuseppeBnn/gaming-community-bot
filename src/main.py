import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from config_data.config import settings
from database.connection import async_session_maker, create_tables, run_migrations
from handlers import (
    admin,
    admin_betting,
    admin_dashboard,
    badges,
    betting,
    common,
    economy,
    fun_ai,
    leaderboard,
    onboarding,
    quiz,
    schedule,
    shop,
)
from handlers.schedule import scheduler_loop
from middlewares.db_middleware import DbSessionMiddleware
from middlewares.group_guard import GroupMemberMiddleware
from middlewares.rate_limit import RateLimitMiddleware
from services import badge_service, catalog_loader

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
    BotCommand(command="trasferisci", description="Trasferisci Aldueuri"),
    BotCommand(command="scommesse", description="Scommesse aperte"),
    BotCommand(command="crea_scommessa", description="Crea una scommessa"),
    BotCommand(command="traguardi", description="I tuoi trofei e rango"),
    BotCommand(command="catalogo_badge", description="Tutti i trofei"),
    BotCommand(command="classifiche", description="Classifiche: ricchezza, XP, trofei"),
    BotCommand(command="negozio", description="Personalizzazioni (tag)"),
    BotCommand(command="help", description="Comandi disponibili"),
]

_GROUP_COMMANDS = [
    BotCommand(command="scommesse", description="Scommesse aperte"),
    BotCommand(command="crea_scommessa", description="Crea una scommessa"),
    BotCommand(command="daily", description="Premio giornaliero"),
    BotCommand(command="saldo", description="Il tuo saldo"),
    BotCommand(command="profilo", description="Il tuo profilo"),
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
    BotCommand(command="help", description="Comandi disponibili"),
]


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

    # Load the customizable CSV catalogs (trophies → DB, ranks/cosmetics → memory),
    # falling back to built-in defaults if the files are absent/invalid.
    async with async_session_maker() as session:
        n_trophies = await badge_service.sync_trophies(session)
    counts = catalog_loader.init_registries()
    logger.info(
        "Cataloghi caricati: %d trofei, %d ranghi, %d cosmetici.",
        n_trophies, counts["ranks"], counts["cosmetics"],
    )

    storage = _build_storage()
    logger.info("FSM storage: %s", settings.fsm_storage)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Middleware order matters: rate-limit first, then DB session, then group guard.
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(GroupMemberMiddleware())

    # Router order matters: admin_betting MUST precede betting so that
    # admin_bet:* callbacks are matched before the catch-all deny at the
    # bottom of admin_betting.router.
    dp.include_router(onboarding.router)
    dp.include_router(economy.router)
    dp.include_router(admin_betting.router)
    dp.include_router(betting.router)
    dp.include_router(badges.router)
    dp.include_router(leaderboard.router)
    dp.include_router(shop.router)
    dp.include_router(admin.router)
    dp.include_router(admin_dashboard.router)
    dp.include_router(quiz.router)
    dp.include_router(schedule.router)
    dp.include_router(fun_ai.router)
    dp.include_router(common.router)

    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    logger.info("Comandi bot registrati.")

    scheduler_task = asyncio.create_task(scheduler_loop(bot))

    logger.info("Bot avviato — polling in corso.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler_task.cancel()
        await bot.session.close()
        logger.info("Bot fermato.")


if __name__ == "__main__":
    asyncio.run(main())
