from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import httpx
from config import settings
from core.app_core import MarzbanError, manager, safe_int_ts
from database.db import db

from aiogram import Bot

logger = logging.getLogger(__name__)


class SubscriptionMaintenanceService:
    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def start(self, bot: Bot | None = None) -> None:
        if bot:
            self.bot = bot
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="subscription-maintenance")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_once(self) -> dict[str, int]:
        stats = {"checked": 0, "expired": 0, "renewed": 0, "failed": 0}
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cfg = await db.get_app_config()
        base_price = cfg["premium_base_price_rub"]

        async for local_user in db.iter_registered_users():
            user_id = local_user["user_id"]
            username = db.build_vpn_username(user_id)
            stats["checked"] += 1
            try:
                vpn_user = await manager.get_user_or_none(username)
                if not vpn_user:
                    continue
                expire_ts = safe_int_ts(vpn_user.get("expire"))

                # Проверка лимита устройств
                try:
                    hwids = await manager.get_user_hwids(username)
                    limit = await db.get_user_device_limit(user_id)
                    count = len(hwids)
                    if count >= limit and await db.should_warn_device_limit(user_id, count, limit):
                        if self.bot:
                            tariff = local_user.get("tariff", "free")
                            tariff_hint = "перейдите на 💎 Премиум для расширения лимита" if tariff == "free" else "обратитесь в поддержку для расширения лимита"
                            warn_msg = (
                                "⚠️ <b>Достигнут лимит устройств!</b>\n\n"
                                f"К вашей подписке подключено максимум устройств: <b>{count} из {limit}</b>.\n\n"
                                "Новые устройства не смогут подключиться к VPN.\n"
                                f"Чтобы освободить место, откройте <b>«📱 Мои устройства»</b> в профиле и удалите неиспользуемое устройство, либо {tariff_hint}."
                            )
                            await self.bot.send_message(user_id, warn_msg)
                        await db.mark_device_limit_warned(user_id, count)
                except Exception as exc:
                    logger.debug("Failed device limit check for %s: %s", username, exc)

                # Предупреждение об окончании тарифа за 24 часа
                if expire_ts and expire_ts > now_ts and expire_ts <= (now_ts + 86400):
                    if local_user.get("warned_expire_ts") != expire_ts:
                        user_tariff = local_user.get("tariff", "free")
                        expire_str = datetime.fromtimestamp(expire_ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
                        if self.bot:
                            try:
                                if user_tariff == "premium":
                                    auto_renew = await db.get_user_auto_renew(user_id)
                                    balance = await db.get_user_balance(user_id)
                                    if auto_renew:
                                        msg = (
                                            "⏳ <b>Премиум-подписка истекает менее чем через 24 часа</b>\n\n"
                                            f"Действует до: <code>{expire_str}</code>\n"
                                            f"У вас <b>включено автопродление</b>. Убедитесь, что баланс не менее <b>{base_price:.2f} ₽</b> "
                                            f"(текущий баланс: <code>{balance:.2f} ₽</code>).\n"
                                            "💎 Подписка будет автоматически продлена на 30 дней для непрерывной работы LTE-обходов."
                                        )
                                    else:
                                        msg = (
                                            "⏳ <b>Премиум-подписка истекает менее чем через 24 часа</b>\n\n"
                                            f"Действует до: <code>{expire_str}</code>\n"
                                            "Продлите Премиум в боте, чтобы сохранить высокую скорость и защиту от блокировок операторов (LTE-обходы)."
                                        )
                                else:
                                    msg = (
                                        "⏳ <b>Бесплатная подписка истекает менее чем через 24 часа</b>\n\n"
                                        f"Действует до: <code>{expire_str}</code>\n"
                                        "Продлите её бесплатно в боте по кнопке <b>«🚀 Подключиться»</b>, либо перейдите на Премиум с технологией LTE-обходов."
                                    )
                                await self.bot.send_message(user_id, msg)
                            except Exception:
                                pass
                        await db.set_user_warned_expire(user_id, expire_ts)

                if expire_ts and expire_ts <= now_ts and vpn_user.get("status") != "expired":
                    # Проверяем возможность автопродления
                    auto_renew = await db.get_user_auto_renew(user_id)
                    user_tariff = local_user.get("tariff", "free")
                    balance = await db.get_user_balance(user_id)

                    if auto_renew and user_tariff == "premium" and balance >= base_price:
                        # Автопродление с баланса
                        await db.change_user_balance(user_id, -base_price)
                        now_dt = datetime.now(timezone.utc)
                        new_expire_dt = now_dt + timedelta(days=30)
                        await db.set_user_tariff(user_id, "premium", new_expire_dt)
                        await manager.upgrade_user_to_premium(username, days=30)
                        stats["renewed"] += 1

                        if self.bot:
                            try:
                                await self.bot.send_message(
                                    user_id,
                                    (
                                        "<b>↺ Автопродление Премиум выполнено</b>\n\n"
                                        f"Списано с баланса: <b>{base_price:.2f} ₽</b>.\n"
                                        "Ваша подписка продлена на <b>30 дней</b> на серверах <b>aska_premium</b>."
                                    ),
                                )
                            except Exception:
                                pass
                        continue

                    # Если автопродление не сработало — переносим в группу aska_renew
                    try:
                        await manager.move_user_to_renew(username)
                    except Exception:
                        await manager.apply_expired_subscription_config(username)

                    await db.mark_subscription_expired(user_id)
                    if user_tariff == "premium":
                        await db.set_user_tariff(user_id, "free")
                    stats["expired"] += 1

                    if self.bot:
                        try:
                            await self.bot.send_message(
                                user_id,
                                (
                                    "<b>• Срок действия подписки истёк</b>\n\n"
                                    "Вы перемещены в группу <b>aska_renew</b>.\n"
                                    "Чтобы продолжить пользоваться VPN на максимальной скорости, продлите Премиум или используйте бесплатный тариф."
                                ),
                            )
                        except Exception:
                            pass
            except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
                stats["failed"] += 1
                logger.warning("Network error for %s — PasarGuard unavailable, will retry next cycle", username)
            except MarzbanError as exc:
                stats["failed"] += 1
                logger.warning("PasarGuard API error for %s: %s", username, exc)
            except Exception:
                stats["failed"] += 1
                logger.exception("Unexpected subscription maintenance error for %s", username)

        return stats

    async def _run(self) -> None:
        interval = max(settings.expired_subscription_check_minutes, 1) * 60
        while not self._stop_event.is_set():
            stats = await self.run_once()
            logger.info("Subscription maintenance finished: %s", stats)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


subscription_maintenance_service = SubscriptionMaintenanceService()
