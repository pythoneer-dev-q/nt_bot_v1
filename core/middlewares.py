from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from database.db import db

logger = logging.getLogger(__name__)


class MaintenanceMiddleware(BaseMiddleware):
    """
    Middleware режима технических работ.
    Когда режим активен (maintenance_mode=True в базе),
    бот полностью игнорирует события обычных пользователей (не отвечает им).
    Администраторы бота продолжают иметь полный доступ.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, Update):
            if event.message and event.message.from_user:
                user_id = event.message.from_user.id
            elif event.callback_query and event.callback_query.from_user:
                user_id = event.callback_query.from_user.id
            elif event.inline_query and event.inline_query.from_user:
                user_id = event.inline_query.from_user.id
            elif event.my_chat_member and event.my_chat_member.from_user:
                user_id = event.my_chat_member.from_user.id
        else:
            from_user = getattr(event, "from_user", None)
            if from_user:
                user_id = getattr(from_user, "id", None)

        if user_id is not None:
            try:
                if await db.is_maintenance_mode():
                    is_admin = await db.is_admin(user_id)
                    if not is_admin:
                        # Режим техработ: бот не отвечает обычному пользователю
                        logger.debug("Maintenance mode active: ignoring event from non-admin user %s", user_id)
                        return None
            except Exception as exc:
                logger.warning("Error checking maintenance mode: %s", exc)

        return await handler(event, data)
