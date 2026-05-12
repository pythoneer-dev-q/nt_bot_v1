import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message
from config import settings
from database.db import db
from utils.kbs import build_inline_markup


class BroadcastService:
    async def send_broadcast(
        self,
        bot: Bot,
        text: str,
        buttons: Sequence[dict[str, str]] | None = None,
        active_only: bool = False,
        status_message: Message | None = None,
    ) -> dict[str, int]:
        payload = {
            "text": text,
            "buttons": list(buttons or []),
            "active_only": active_only,
            "started_at": datetime.now(timezone.utc),
        }
        broadcast = await db.create_broadcast(payload)
        stats = {"sent": 0, "failed": 0, "blocked": 0}
        batch: list[dict] = []

        async for user in db.iter_users(active_only=active_only):
            batch.append(user)
            if len(batch) >= settings.broadcast_batch_size:
                partial = await self._dispatch_batch(bot, batch, text, buttons)
                self._merge_stats(stats, partial)
                await self._update_status(status_message, stats)
                batch.clear()

        if batch:
            partial = await self._dispatch_batch(bot, batch, text, buttons)
            self._merge_stats(stats, partial)
            await self._update_status(status_message, stats)

        await db.finish_broadcast(broadcast["_id"], stats)
        return stats

    async def _dispatch_batch(
        self,
        bot: Bot,
        users_batch: Sequence[dict],
        text: str,
        buttons: Sequence[dict[str, str]] | None,
    ) -> dict[str, int]:
        semaphore = asyncio.Semaphore(settings.broadcast_concurrency)

        async def runner(user: dict) -> str:
            async with semaphore:
                return await self._send_one(bot, user, text, buttons)

        results = await asyncio.gather(*(runner(user) for user in users_batch))
        stats = {"sent": 0, "failed": 0, "blocked": 0}
        for result in results:
            stats[result] += 1
        return stats

    async def _send_one(
        self,
        bot: Bot,
        user: dict,
        text: str,
        buttons: Sequence[dict[str, str]] | None,
    ) -> str:
        markup = build_inline_markup(buttons or [])
        try:
            await bot.send_message(chat_id=user["user_id"], text=text, reply_markup=markup)
            await db.touch_broadcast_delivery(user["user_id"])
            return "sent"
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            return await self._send_one(bot, user, text, buttons)
        except TelegramForbiddenError:
            await db.mark_user_blocked(user["user_id"], True)
            return "blocked"
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower():
                await db.mark_user_blocked(user["user_id"], True)
                return "blocked"
            return "failed"
        except Exception:
            return "failed"

    async def _update_status(self, status_message: Message | None, stats: dict[str, int]) -> None:
        if not status_message:
            return
        total = stats["sent"] + stats["failed"] + stats["blocked"]
        await status_message.edit_text(
            "Рассылка выполняется...\n"
            f"Доставлено: {stats['sent']}\n"
            f"Заблокировали бота: {stats['blocked']}\n"
            f"Ошибки: {stats['failed']}\n"
            f"Обработано: {total}"
        )

    @staticmethod
    def _merge_stats(target: dict[str, int], source: dict[str, int]) -> None:
        for key, value in source.items():
            target[key] += value


broadcast_service = BroadcastService()
