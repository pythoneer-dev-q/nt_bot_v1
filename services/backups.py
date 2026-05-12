from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from database.db import db
from database.migration import MIGRATION_COLLECTIONS, export_database


logger = logging.getLogger(__name__)


class BackupService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if not settings.backup_enabled or self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="daily-db-backups")

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

    async def run_once(self) -> Path:
        path = await export_database()
        await db.record_backup_run(str(path), list(MIGRATION_COLLECTIONS), ok=True)
        await self._cleanup_old_backups(path.parent)
        return path

    async def _run(self) -> None:
        interval = max(settings.backup_interval_hours, 1) * 3600
        while not self._stop_event.is_set():
            try:
                path = await self.run_once()
                logger.info("Database backup created at %s", path)
            except Exception as exc:
                logger.exception("Database backup failed")
                try:
                    await db.record_backup_run("", list(MIGRATION_COLLECTIONS), ok=False, error=str(exc))
                except Exception:
                    logger.exception("Failed to record backup failure")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _cleanup_old_backups(self, root: Path) -> None:
        if settings.backup_keep_days <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.backup_keep_days)
        for item in root.iterdir():
            if not item.is_dir():
                continue
            created_at = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
            if created_at < cutoff:
                shutil.rmtree(item)


backup_service = BackupService()
