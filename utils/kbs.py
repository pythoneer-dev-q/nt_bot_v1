from collections.abc import Sequence
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database.db import db
from config import settings


async def _append_ad_row(builder: InlineKeyboardBuilder) -> None:
    # Рекламная кнопка добавляется отдельной строкой, чтобы не ломать сетку основных action-кнопок.
    try:
        ad = await db.get_random_ad()
    except AssertionError:
        return
    if not ad:
        return
    builder.row(InlineKeyboardButton(text=ad["text"], url=ad["url"], style="success"))


def build_inline_markup(buttons: Sequence[dict[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None

    builder = InlineKeyboardBuilder()
    for button in buttons:
        kind = button.get("type", "url")
        if kind == "callback":
            builder.button(text=button["text"], callback_data=button["value"], style=button.get("style"))
        else:
            builder.button(text=button["text"], url=button["value"], style=button.get("style"))
    builder.adjust(1)
    return builder.as_markup()


async def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Подключиться", callback_data="user:sub", style="primary")
    builder.button(text="🧩 FAQ", callback_data="user:faq")
    builder.button(text="👤 Профиль", callback_data="user:profile")
    builder.button(text="💡 Поддержка", callback_data="user:support", style="primary")
    builder.button(text="⚡️ Статистика", callback_data="user:stats")
    builder.button(text="📊 Подписка", callback_data="user:sub")
    builder.button(text="❓ Почему я вижу рекламу", callback_data="user:donate")
    builder.adjust(1, 2, 2, 1, 1)
    await _append_ad_row(builder)
    return builder.as_markup()


async def registration_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Ссылки на документы вынесены в env, чтобы можно было менять их без правки кода.
    if settings.user_agreement_url:
        builder.row(
            InlineKeyboardButton(
                text="📄 Пользовательское соглашение",
                url=settings.user_agreement_url,
            )
        )
    if settings.privacy_policy_url:
        builder.row(
            InlineKeyboardButton(
                text="🛡 Политика конфиденциальности",
                url=settings.privacy_policy_url,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="✅ Принимаю условия",
            callback_data="user:accept_rules",
            style="success",
        )
    )
    return builder.as_markup()


async def subscription_keyboard(url: str, show_free_renew: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    url = f'{settings.marzban_url}{url.replace('/', '', 1)}'
    if url:
        builder.row(InlineKeyboardButton(text="🚀 Открыть подписку", url=url, style="primary"))
    if show_free_renew:
        builder.row(InlineKeyboardButton(text="🔄 Продлить бесплатно", callback_data="user:renew_free", style="success"))
    builder.row(InlineKeyboardButton(text="📱 Устройства", callback_data="user:devices"), InlineKeyboardButton(text="🕓 Действия", callback_data="user:sub_updates"))
    builder.row(InlineKeyboardButton(text="🔐 Сброс ссылки", callback_data="user:revoke_sub", style="danger"))
    builder.row(InlineKeyboardButton(text="🗯 Инструкция в боте", callback_data="user:instruction"), InlineKeyboardButton(text="↖️ Назад", callback_data="user:menu"))
    if settings.help_url:
        builder.row(InlineKeyboardButton(text="🛠 Инструкция", url=settings.help_url, style="success"))
    await _append_ad_row(builder)
    return builder.as_markup()


async def devices_keyboard(devices: Sequence[dict], back_to: str = "user:sub") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, device in enumerate(devices[:8]):
        label = device.get("device_model") or device.get("device_os") or device.get("hwid") or f"#{index + 1}"
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {str(label)[:32]}",
                callback_data=f"user:device_del:{index}",
                style="danger",
            )
        )
    if devices:
        builder.row(InlineKeyboardButton(text="🧹 Сбросить все устройства", callback_data="user:devices_reset", style="danger"))
    builder.row(InlineKeyboardButton(text="♻️ Обновить", callback_data="user:devices"))
    builder.row(InlineKeyboardButton(text="↖️ К подписке", callback_data=back_to))
    return builder.as_markup()


async def revoke_subscription_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔐 Сбросить ссылку", callback_data="user:revoke_sub_confirm", style="danger"))
    builder.row(InlineKeyboardButton(text="↖️ К подписке", callback_data="user:sub"))
    return builder.as_markup()


async def faq_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❓ Почему в боте есть реклама", callback_data="user:donate"))
    builder.row(InlineKeyboardButton(text="❤️ Как помочь проекту", callback_data="user:donate_help", style="success"))
    builder.row(InlineKeyboardButton(text="⚡️ Хочу платный тариф", callback_data="user:support", style="primary"))
    builder.row(InlineKeyboardButton(text="↖️ Назад", callback_data="user:menu"))
    return builder.as_markup()


async def profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↖️ Назад", callback_data="user:menu"))
    return builder.as_markup()


async def support_page_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❓ Создать вопрос", callback_data="user:start_support", style="success"))
    if settings.support_url:
        builder.row(InlineKeyboardButton(text="💬 Написать напрямую", url=settings.support_url, style="primary"))
    elif settings.support_username:
        builder.row(
            InlineKeyboardButton(
                text="💬 Написать напрямую",
                url=f"https://t.me/{settings.support_username.lstrip('@')}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="↖️ Назад", callback_data="user:menu"))
    return builder.as_markup()


async def donate_keyboard(back_to: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if settings.donate_card:
        builder.row(
            InlineKeyboardButton(
                text="💳 Копировать карту",
                copy_text=CopyTextButton(text=settings.donate_card),
                style="success",
            )
        )
    if settings.donate_url:
        builder.row(InlineKeyboardButton(text="❤️ Донат в один клик", url=settings.donate_url, style="primary"))
    builder.row(InlineKeyboardButton(text="↖️ Назад", callback_data=back_to))
    return builder.as_markup()


async def stats_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↖️ Назад", callback_data="user:menu"))
    return builder.as_markup()


async def instruction_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↖️ К подписке", callback_data="user:sub", style="success"))
    builder.row(InlineKeyboardButton(text="↖️ В меню", callback_data="user:menu"))
    return builder.as_markup()


async def sponsors_keyboard(channels: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        if channel.get("url"):
            builder.row(
                InlineKeyboardButton(
                    text=f"⚡️ Подписаться: {channel.get('title', channel['channel_ref'])}",
                    url=channel["url"],
                    style="primary",
                )
            )
    builder.row(InlineKeyboardButton(text="✅ Подписался", callback_data="user:check_sub", style="success"))
    return builder.as_markup()


async def support_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Отправить", callback_data="support:send", style="success"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="support:cancel", style="danger"))
    return builder.as_markup()


async def admin_panel() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Rescue", callback_data="admin:rescue", style="danger")
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="👤 Найти пользователя", callback_data="admin:user_lookup")
    builder.button(text="📨 Рассылки", callback_data="admin:broadcast", style="primary")
    builder.button(text="📣 Каналы", callback_data="admin:channels")
    builder.button(text="🗞 Реклама", callback_data="admin:ads")
    builder.button(text="➕ Добавить админа", callback_data="admin:add_admin", style="primary")
    builder.button(text="🛡 Список админов", callback_data="admin:list_admins")
    builder.button(text="👀 В меню как пользователь", callback_data="user:menu", style="success")
    builder.adjust(2, 2, 2, 2)
    return builder.as_markup()


async def admin_user_actions(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Block with reason", callback_data=f"admin_user:block:{user_id}", style="danger")
    builder.button(text="Unblock", callback_data=f"admin_user:unblock:{user_id}", style="success")
    builder.button(text="📅 +30 дней", callback_data=f"admin_user:extend30:{user_id}", style="success")
    builder.button(text="🗓 +90 дней", callback_data=f"admin_user:extend90:{user_id}", style="success")
    builder.button(text="✅ Активировать", callback_data=f"admin_user:activate:{user_id}", style="primary")
    builder.button(text="⛔ Отключить", callback_data=f"admin_user:disable:{user_id}", style="danger")
    builder.button(text="🔄 Сбросить трафик", callback_data=f"admin_user:reset:{user_id}")
    builder.button(text="🔐 Сброс ссылки", callback_data=f"admin_user:revoke_sub:{user_id}", style="danger")
    builder.button(text="📱 Устройства", callback_data=f"admin_user:devices:{user_id}")
    builder.button(text="⏭ След. план", callback_data=f"admin_user:active_next:{user_id}", style="primary")
    builder.button(text="♻️ Обновить", callback_data=f"admin_user:refresh:{user_id}")
    builder.button(text="⬅️ Назад", callback_data="admin:panel")
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


async def admin_user_devices(user_id: int, devices: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, device in enumerate(devices[:8]):
        label = device.get("device_model") or device.get("device_os") or device.get("hwid") or f"#{index + 1}"
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {str(label)[:32]}",
                callback_data=f"admin_user:device_del:{user_id}:{index}",
                style="danger",
            )
        )
    if devices:
        builder.row(InlineKeyboardButton(text="🧹 Сбросить все", callback_data=f"admin_user:devices_reset:{user_id}", style="danger"))
    builder.row(InlineKeyboardButton(text="♻️ Обновить", callback_data=f"admin_user:devices:{user_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"admin_user:refresh:{user_id}"))
    return builder.as_markup()


async def admin_rescue_panel() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Health check", callback_data="admin:health"))
    builder.row(InlineKeyboardButton(text="🚀 Подключиться", callback_data="user:sub", style="primary"))
    builder.row(InlineKeyboardButton(text="Set Subscription alert", callback_data="admin:set_subscription_alert", style="primary"))
    builder.row(InlineKeyboardButton(text="Clear Subscription alert", callback_data="admin:clear_subscription_alert", style="danger"))
    builder.row(InlineKeyboardButton(text="Create backup now", callback_data="admin:backup_now", style="success"))
    builder.row(InlineKeyboardButton(text="Export for migration", callback_data="admin:export_migration", style="primary"))
    builder.row(InlineKeyboardButton(text="Repair expired subscriptions", callback_data="admin:subscription_maintenance"))
    builder.row(InlineKeyboardButton(text="Back", callback_data="admin:panel"))
    return builder.as_markup()


async def broadcast_menu(buttons: Sequence[dict[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="broadcast:add_button", style="primary"))
    builder.row(InlineKeyboardButton(text="📨 Отправить всем", callback_data="broadcast:send_all", style="success"))
    builder.row(InlineKeyboardButton(text="🎯 Только активным", callback_data="broadcast:send_active"))
    if buttons:
        builder.row(InlineKeyboardButton(text="🧹 Очистить кнопки", callback_data="broadcast:clear_buttons", style="danger"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel"))
    return builder.as_markup()


async def admins_list(admins: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for admin in admins:
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 Удалить {admin['user_id']}",
                callback_data=f"admin:delete_admin:{admin['user_id']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel"))
    return builder.as_markup()


async def sponsor_channels_list(channels: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin:add_channel", style="primary"))
    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {channel.get('title', channel['channel_ref'])}",
                callback_data=f"admin:delete_channel:{channel['channel_ref']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel"))
    return builder.as_markup()


async def ads_list_keyboard(ads: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить рекламо-кнопку", callback_data="admin:add_ad", style="primary"))
    for ad in ads:
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 {ad['text']}",
                callback_data=f"admin:delete_ad:{ad['_id']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel"))
    return builder.as_markup()


async def support_request_keyboard(user_id: int, ticket_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"✅ Ответить [{user_id}]",
            callback_data=f"support:answer:{user_id}:{ticket_id}",
            style="success",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Отклонить обращение",
            callback_data=f"support:decline:{user_id}:{ticket_id}",
            style="danger",
        )
    )
    return builder.as_markup()
