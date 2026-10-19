import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from config import settings
from core.app_core import manager
from core.middlewares import MaintenanceMiddleware
from database.db import db
from handlers.admin_router import router as admin_router
from handlers.fallback_router import router as fallback_router
from handlers.user_router import router as user_router
from services.backups import backup_service
from services.payments import payment_monitor_service, payment_service
from services.subscriptions import subscription_maintenance_service


def build_storage():
    if not settings.redis_dsn:
        return MemoryStorage()

    try:
        from redis.asyncio import Redis
        from aiogram.fsm.storage.redis import RedisStorage
    except ImportError:
        logging.warning("REDIS_DSN is set, but redis dependencies are missing. MemoryStorage will be used.")
        return MemoryStorage()

    redis = Redis.from_url(settings.redis_dsn, encoding="utf-8", decode_responses=True)
    return RedisStorage(redis=redis)


async def on_startup(bot: Bot) -> None:
    await db.connect()
    await db.ensure_indexes()
    backup_service.start()
    subscription_maintenance_service.start(bot)
    payment_monitor_service.start(bot)


async def on_shutdown() -> None:
    await payment_monitor_service.stop()
    await subscription_maintenance_service.stop()
    await backup_service.stop()
    await payment_service.close()
    await manager.close()
    await db.close()


async def main() -> None:
    if settings.disable_logs:
        logging.basicConfig(level=logging.CRITICAL)
        logging.getLogger().setLevel(logging.CRITICAL)
    else:
        log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        if not settings.debug:
            logging.getLogger("aiogram").setLevel(logging.WARNING)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("aiohttp").setLevel(logging.WARNING)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=build_storage())
    dp.update.outer_middleware(MaintenanceMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(fallback_router)

    await on_startup(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
