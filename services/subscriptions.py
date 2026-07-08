from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import settings
from core.app_core import MarzbanError, manager
from database.db import db


logger = logging.getLogger(__name__)


class SubscriptionMaintenanceService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
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
        stats = {"checked": 0, "expired": 0, "failed": 0}
        now_ts = int(datetime.now(timezone.utc).timestamp())

        async for local_user in db.iter_registered_users():
            user_id = local_user["user_id"]
            username = db.build_vpn_username(user_id)
            stats["checked"] += 1
            try:
                vpn_user = await manager.get_user_or_none(username)
                if not vpn_user:
                    continue
                expire_ts = manager._normalize_expire(vpn_user.get("expire")) or 0
                if expire_ts and expire_ts <= now_ts and vpn_user.get("status") != "expired":
                    await manager.apply_expired_subscription_config(username)
                    await db.mark_subscription_expired(user_id)
                    stats["expired"] += 1
            except MarzbanError:
                stats["failed"] += 1
                logger.exception("Failed to process expired subscription for %s", username)
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
