from aiogram import Router
from aiogram.types import CallbackQuery
from config import settings
from utils import kbs

router = Router(name="fallback")


def _main_menu_text() -> str:
    if settings.menu_image_url:
        return f'<b><a href="{settings.menu_image_url}">⚡️</a> Главное меню</b>'
    return f"<b>{settings.project_name}</b>"


@router.callback_query()
async def unknown_callback(call: CallbackQuery) -> None:
    # Этот обработчик идет последним и ловит только неизвестные/устаревшие callback-кнопки.
    try:
        await call.message.edit_text(_main_menu_text(), reply_markup=await kbs.main_menu())
    except Exception:
        await call.message.answer(_main_menu_text(), reply_markup=await kbs.main_menu())
    await call.answer("Такой кнопки не существует", show_alert=False)
