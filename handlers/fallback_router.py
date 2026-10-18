from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from config import settings
from database.db import db
from utils import kbs

router = Router(name="fallback")


async def _main_menu_text(user_tariff: str = "free") -> str:
    cfg = await db.get_app_config()
    project_name = cfg.get("project_name", settings.project_name)
    menu_desc = cfg.get("menu_description", settings.menu_description)
    img_part = f'<a href="{settings.menu_image_url}">&#8205;</a>' if settings.menu_image_url else ""

    return (
        f"{img_part}<b>{project_name}</b>\n\n"
        f"<blockquote>{menu_desc}</blockquote>\n\n"
        "Выберите нужный раздел в меню:"
    )


@router.callback_query()
async def unknown_callback(call: CallbackQuery) -> None:
    tariff = await db.get_user_tariff(call.from_user.id)
    text = await _main_menu_text(tariff)
    markup = await kbs.main_menu(tariff)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)
    await call.answer("Такой кнопки не существует или она устарела", show_alert=False)
