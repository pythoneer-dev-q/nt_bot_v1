from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from database.db import db


class AdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        else:
            user = getattr(event, "from_user", None)

        if not user:
            return False
        return await db.is_admin(user.id)
