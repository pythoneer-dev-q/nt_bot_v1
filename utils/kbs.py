from __future__ import annotations

from collections.abc import Sequence
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import settings
from core.tariffs import TariffOption
from database.db import db


async def _append_ad_row(builder: InlineKeyboardBuilder) -> None:
    try:
        ad = await db.get_random_ad()
    except Exception:
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


async def main_menu(user_tariff: str = "free") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Подключиться", callback_data="user:connect", style="primary")
    builder.button(text="👤 Профиль", callback_data="user:profile")
    builder.button(text="ℹ️ Справка", callback_data="user:faq")
    builder.button(text="✉️ Поддержка", callback_data="user:support", style="primary")
    builder.button(text="📊 Статистика", callback_data="user:stats")
    builder.adjust(1, 2, 2)
    return builder.as_markup()


async def connect_choice_keyboard() -> InlineKeyboardMarkup:
    """Выбор между бесплатным и платным тарифом при подключении."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🆓 Бесплатный тариф", callback_data="user:connect:free", style="primary"))
    builder.row(InlineKeyboardButton(text="💎 Премиум тариф (LTE)", callback_data="user:connect:premium", style="success"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def ad_interstitial_keyboard(ad_url: str, ad_text: str = "Перейти к спонсору") -> InlineKeyboardMarkup:
    """Клавиатура для 3-секундного показа рекламы перед переходом к подписке."""
    builder = InlineKeyboardBuilder()
    if ad_url:
        builder.row(InlineKeyboardButton(text=f"➡️ {ad_text}", url=ad_url, style="success"))
    return builder.as_markup()


async def tariffs_keyboard(
    tariffs: list[TariffOption],
    active_tariff: str = "free",
    can_overdraft: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in tariffs:
        builder.row(InlineKeyboardButton(text=f"{option.button_text} (LTE-обход)", callback_data=f"pay:select:{option.period_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def topup_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора суммы пополнения баланса: кнопки фиксированных сумм + своя сумма."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💳 100 ₽", callback_data="user:topup_amount:100"),
        InlineKeyboardButton(text="💳 300 ₽", callback_data="user:topup_amount:300"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 500 ₽", callback_data="user:topup_amount:500"),
        InlineKeyboardButton(text="💳 1000 ₽", callback_data="user:topup_amount:1000"),
    )
    builder.row(InlineKeyboardButton(text="✏️ Ввести свою сумму", callback_data="user:topup_custom", style="primary"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def tariff_pay_options_keyboard(
    period_id: str,
    price: float,
    can_pay_balance: bool = False,
) -> InlineKeyboardMarkup:
    """Выбор способа оплаты: с баланса или через платёжную систему."""
    builder = InlineKeyboardBuilder()
    if can_pay_balance:
        builder.row(InlineKeyboardButton(text=f"💰 Оплатить с баланса ({price} ₽)", callback_data=f"pay:balance:{period_id}", style="success"))
    builder.row(InlineKeyboardButton(text=f"💳 Оплатить через СБП/Крипту ({price} ₽)", callback_data=f"pay:start:{period_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="« Назад к тарифам", callback_data="user:tariffs"))
    return builder.as_markup()


async def free_connect_keyboard(
    subscription_url: str,
    ad: dict | None = None,
    show_free_renew: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if subscription_url:
        builder.row(InlineKeyboardButton(text="🚀 Открыть подписку", url=subscription_url, style="primary"))
    builder.row(InlineKeyboardButton(text="⚙️ Настройки подписки", callback_data="user:sub_settings"))
    if show_free_renew:
        builder.row(InlineKeyboardButton(text="🔄 Продлить бесплатно", callback_data="user:renew_free", style="success"))

    builder.row(InlineKeyboardButton(text="💎 Перейти на Премиум (LTE)", callback_data="user:tariffs", style="primary"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def premium_connect_keyboard(
    subscription_url: str,
    auto_renew: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if subscription_url:
        builder.row(InlineKeyboardButton(text="🚀 Открыть подписку (Премиум)", url=subscription_url, style="primary"))
    
    builder.row(InlineKeyboardButton(text="⚙️ Настройки подписки", callback_data="user:sub_settings"))
    builder.row(InlineKeyboardButton(text="➕ Продлить Премиум", callback_data="user:tariffs", style="success"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def payment_keyboard(
    pay_url: str,
    order_id: str,
    amount: int | float,
    back_callback: str = "user:tariffs",
    back_text: str = "« Назад к тарифам",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if pay_url:
        builder.row(InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=pay_url, style="primary"))
    builder.row(InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"pay:check:{order_id}", style="success"))
    builder.row(InlineKeyboardButton(text=back_text, callback_data=back_callback))
    return builder.as_markup()


async def registration_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
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
                text="🔒 Политика конфиденциальности",
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


async def subscription_keyboard(
    url: str,
    is_premium: bool = False,
    show_free_renew: bool = False,
    auto_renew: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if url:
        builder.row(InlineKeyboardButton(text="🚀 Открыть подписку", url=url, style="primary"))
    
    builder.row(InlineKeyboardButton(text="⚙️ Настройки подписки", callback_data="user:sub_settings"))
    if is_premium:
        renew_icon = "ВКЛ" if auto_renew else "ВЫКЛ"
        builder.row(InlineKeyboardButton(text=f"🔄 Автопродление: [{renew_icon}]", callback_data="user:toggle_autorenew"))
        builder.row(InlineKeyboardButton(text="➕ Продлить Премиум", callback_data="user:tariffs", style="success"))
    else:
        if show_free_renew:
            builder.row(InlineKeyboardButton(text="🔄 Продлить бесплатно", callback_data="user:renew_free", style="success"))
        builder.row(InlineKeyboardButton(text="💎 Перейти на Премиум (LTE)", callback_data="user:tariffs", style="primary"))

    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def sub_settings_keyboard(
    is_premium: bool = False,
    auto_renew: bool = True,
    devices_count: int = 0,
    device_limit: int = 1,
) -> InlineKeyboardMarkup:
    """Клавиатура настроек подписки: управление устройствами, сброс ссылки, автопродление."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"📱 Мои устройства ({devices_count}/{device_limit})", callback_data="user:devices"))
    builder.row(InlineKeyboardButton(text="🔄 Сбросить ссылку подписки", callback_data="user:reset_sub"))
    if is_premium:
        renew_icon = "ВКЛ" if auto_renew else "ВЫКЛ"
        builder.row(InlineKeyboardButton(text=f"🔄 Автопродление: [{renew_icon}]", callback_data="user:toggle_autorenew"))
    builder.row(InlineKeyboardButton(text="« Назад к подключению", callback_data="user:connect"))
    return builder.as_markup()


async def faq_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💎 Тарифы и цены", callback_data="user:tariffs", style="primary"))
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="user:menu"))
    return builder.as_markup()


async def profile_keyboard(
    can_overdraft: bool = False,
    *args,
    **kwargs,
) -> InlineKeyboardMarkup:
    """Клавиатура профиля (баланс, история платежей, перерасход)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="user:topup", style="primary"))
    builder.row(InlineKeyboardButton(text="📜 История платежей", callback_data="user:history"))
    if can_overdraft:
        builder.row(InlineKeyboardButton(text="⚡️ Перерасход (+3 дня)", callback_data="user:overdraft", style="success"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def devices_keyboard(hwids: list[dict], device_limit: int) -> InlineKeyboardMarkup:
    """Клавиатура списка устройств: клик на устройство открывает его карточку."""
    builder = InlineKeyboardBuilder()
    for idx, dev in enumerate(hwids, start=1):
        hwid = dev.get("hwid", "")
        short_hwid = hwid[:24]
        model = dev.get("device_model") or dev.get("device_os") or f"Устройство {idx}"
        os_name = dev.get("device_os") or ""
        label = f"{model} ({os_name})" if os_name and os_name != model else model
        builder.row(
            InlineKeyboardButton(
                text=f"📱 {label[:24]}",
                callback_data=f"user:dev:{short_hwid}",
            )
        )
    if len(hwids) > 1:
        builder.row(InlineKeyboardButton(text="🔄 Сбросить все устройства", callback_data="user:dev_reset_all", style="danger"))

    builder.row(InlineKeyboardButton(text="« В настройки подписки", callback_data="user:sub_settings"))
    return builder.as_markup()


async def device_details_keyboard(short_hwid: str) -> InlineKeyboardMarkup:
    """Клавиатура карточки отдельного устройства: удалить или вернуться."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Удалить устройство", callback_data=f"user:dev_del:{short_hwid}", style="danger"))
    builder.row(InlineKeyboardButton(text="« Назад к устройствам", callback_data="user:devices"))
    builder.row(InlineKeyboardButton(text="« В настройки подписки", callback_data="user:sub_settings"))
    return builder.as_markup()


async def device_delete_confirm_keyboard(hwid: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    short_hwid = hwid[:24]
    builder.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"user:dev_del_conf:{short_hwid}", style="danger"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"user:dev:{short_hwid}"),
    )
    return builder.as_markup()


async def device_reset_all_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, удалить ВСЕ", callback_data="user:dev_reset_all_conf", style="danger"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="user:devices"),
    )
    return builder.as_markup()


async def reset_sub_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, сбросить ссылку", callback_data="user:reset_sub_conf", style="danger"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="user:sub_settings"),
    )
    return builder.as_markup()


async def payment_history_keyboard(
    payments: list[dict],
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Список пополнений/платежей пользователя с пагинацией."""
    builder = InlineKeyboardBuilder()
    status_icon_map = {
        "paid": "✅",
        "refund_requested": "⏳",
        "refunded": "🔄",
        "refund_declined": "❌",
        "created": "⏳",
        "canceled": "❌",
        "expired": "❌",
    }
    for p in payments:
        amount = p.get("amount", 0.0)
        status = p.get("status", "unknown")
        icon = status_icon_map.get(status, "💳")
        order_id = p.get("order_id", "")
        title = f"💳 {amount:.0f} ₽ [{icon}] #{order_id[-6:]}"
        builder.row(InlineKeyboardButton(text=title, callback_data=f"user:order:{order_id}"))

    if total_pages > 1:
        prev_page = page - 1
        next_page = page + 1
        nav_buttons = []
        if prev_page >= 1:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"user:history:{prev_page}"))
        else:
            nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="noop"))

        nav_buttons.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="noop"))

        if next_page <= total_pages:
            nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"user:history:{next_page}"))
        else:
            nav_buttons.append(InlineKeyboardButton(text="⏹", callback_data="noop"))
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="« Личный кабинет", callback_data="user:profile"))
    return builder.as_markup()


async def order_details_keyboard(order: dict) -> InlineKeyboardMarkup:
    """Карточка платежа с кнопкой запроса отмены (если оплачен)."""
    builder = InlineKeyboardBuilder()
    order_id = order.get("order_id", "")
    status = order.get("status", "")

    if status == "paid":
        builder.row(InlineKeyboardButton(text="🔄 Запросить отмену платежа", callback_data=f"pay:cancel_req:{order_id}", style="danger"))
    elif status == "created":
        pay_url = order.get("pay_url")
        if pay_url:
            builder.row(InlineKeyboardButton(text=f"💳 Оплатить {order.get('amount', 0)} ₽", url=pay_url, style="primary"))
        builder.row(InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"pay:check:{order_id}", style="success"))

    builder.row(InlineKeyboardButton(text="« К истории пополнений", callback_data="user:history"))
    return builder.as_markup()


async def cancel_request_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➡️ Отправить без описания", callback_data=f"pay:cancel_submit:{order_id}", style="primary"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"user:order:{order_id}"))
    return builder.as_markup()


async def admin_refund_action_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить отмену", callback_data=f"admin_refund:approve:{order_id}", style="success"),
        InlineKeyboardButton(text="❌ Отклонить отмену", callback_data=f"admin_refund:decline:{order_id}", style="danger"),
    )
    builder.row(InlineKeyboardButton(text="« К списку запросов", callback_data="admin:refund_requests"))
    return builder.as_markup()


async def admin_refund_done_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="« К списку запросов", callback_data="admin:refund_requests"))
    builder.row(InlineKeyboardButton(text="« В админ-панель", callback_data="admin:panel"))
    return builder.as_markup()


async def admin_refund_requests_list_keyboard(requests: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for req in requests:
        order_id = req.get("order_id", "")
        amount = req.get("amount", 0.0)
        user_id = req.get("user_id", "")
        builder.row(InlineKeyboardButton(
            text=f"🔄 #{order_id[-6:]} | {amount:.0f} ₽ (ID {user_id})",
            callback_data=f"admin_refund:view:{order_id}",
        ))
    builder.row(InlineKeyboardButton(text="« В админ-панель", callback_data="admin:panel"))
    return builder.as_markup()


SUPPORT_TOPIC_MAP: dict[str, str] = {
    "payment": "💳 Проблема с оплатой",
    "connect": "🚀 Подключение к VPN",
    "devices": "📱 Настройка устройств",
    "premium": "💎 Тариф Премиум / LTE",
    "general": "❓ Общий вопрос",
}


async def support_page_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Создать обращение", callback_data="user:start_support", style="success"))
    if settings.support_url:
        builder.row(InlineKeyboardButton(text="✉️ Написать напрямую", url=settings.support_url, style="primary"))
    elif settings.support_username:
        builder.row(
            InlineKeyboardButton(
                text="✉️ Написать напрямую",
                url=f"https://t.me/{settings.support_username.lstrip('@')}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    return builder.as_markup()


async def support_topics_keyboard() -> InlineKeyboardMarkup:
    """Выбор готовой темы обращения или ввод темы вручную."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Проблема с оплатой", callback_data="support:topic:payment"))
    builder.row(InlineKeyboardButton(text="🚀 Подключение к VPN", callback_data="support:topic:connect"))
    builder.row(InlineKeyboardButton(text="📱 Настройка устройств", callback_data="support:topic:devices"))
    builder.row(InlineKeyboardButton(text="💎 Тариф Премиум / LTE", callback_data="support:topic:premium"))
    builder.row(InlineKeyboardButton(text="❓ Общий вопрос", callback_data="support:topic:general"))
    builder.row(InlineKeyboardButton(text="✏️ Другая тема (ввести вручную)", callback_data="support:topic:custom", style="primary"))
    builder.row(InlineKeyboardButton(text="« Отмена", callback_data="support:cancel"))
    return builder.as_markup()


async def support_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой отмены в процессе создания обращения."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="« Отмена", callback_data="support:cancel", style="danger"))
    return builder.as_markup()


async def stats_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="user:menu"))
    return builder.as_markup()


async def instruction_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🚀 К подключению", callback_data="user:connect", style="success"))
    builder.row(InlineKeyboardButton(text="« В меню", callback_data="user:menu"))
    return builder.as_markup()


async def sponsors_keyboard(channels: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        if channel.get("url"):
            builder.row(
                InlineKeyboardButton(
                    text=f"📢 Подписаться: {channel.get('title', channel['channel_ref'])}",
                    url=channel["url"],
                    style="primary",
                )
            )
    builder.row(InlineKeyboardButton(text="✅ Подписался", callback_data="user:check_sub", style="success"))
    builder.row(InlineKeyboardButton(text="💎 Премиум без каналов (LTE)", callback_data="user:tariffs", style="primary"))
    return builder.as_markup()


async def support_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Отправить", callback_data="support:send", style="success"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="support:cancel", style="danger"))
    return builder.as_markup()


# ---------------- Admin Keyboards ----------------
async def admin_panel() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Тарифы и цены", callback_data="admin:pricing", style="success")
    builder.button(text="📱 Лимиты устройств", callback_data="admin:device_limits", style="primary")
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="🔍 Найти пользователя", callback_data="admin:user_lookup")
    builder.button(text="✉️ Рассылки", callback_data="admin:broadcast", style="primary")
    builder.button(text="📢 Каналы", callback_data="admin:channels")
    builder.button(text="🎯 Реклама", callback_data="admin:ads")
    builder.button(text="🔄 Запросы отмен", callback_data="admin:refund_requests", style="primary")
    builder.button(text="➕ Добавить админа", callback_data="admin:add_admin")
    builder.button(text="👥 Список админов", callback_data="admin:list_admins")
    builder.button(text="⚠️ Rescue", callback_data="admin:rescue", style="danger")
    builder.button(text="« Меню пользователя", callback_data="user:menu", style="success")
    builder.adjust(2, 2, 2, 2, 2, 2)
    return builder.as_markup()


async def admin_pricing_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Базовая цена (1 мес)", callback_data="admin:set_base_price", style="primary"))
    builder.row(InlineKeyboardButton(text="✏️ Процент скидки", callback_data="admin:set_discount_percent", style="primary"))
    builder.row(InlineKeyboardButton(text="« В админ-панель", callback_data="admin:panel"))
    return builder.as_markup()


async def admin_device_limits_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Лимит устройств (Free)", callback_data="admin:set_free_device_limit", style="primary"))
    builder.row(InlineKeyboardButton(text="✏️ Лимит устройств (Premium)", callback_data="admin:set_premium_device_limit", style="primary"))
    builder.row(InlineKeyboardButton(text="« В админ-панель", callback_data="admin:panel"))
    return builder.as_markup()


async def admin_user_actions(user_id: int, is_premium: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_premium:
        builder.button(text="👤 Сделать Free", callback_data=f"admin_user:make_free:{user_id}")
    else:
        builder.button(text="💎 Сделать Premium", callback_data=f"admin_user:make_premium:{user_id}", style="primary")

    builder.button(text="📱 Лимит устройств", callback_data=f"admin_user:set_limit:{user_id}", style="primary")
    builder.button(text="🔄 Сбросить HWID", callback_data=f"admin_user:reset_hwids:{user_id}")
    builder.button(text="🔄 Сбросить ссылку", callback_data=f"admin_user:revoke_sub:{user_id}")
    builder.button(text="🚫 Заблокировать", callback_data=f"admin_user:block:{user_id}", style="danger")
    builder.button(text="✅ Разблокировать", callback_data=f"admin_user:unblock:{user_id}", style="success")
    builder.button(text="➕ 30 дней", callback_data=f"admin_user:extend30:{user_id}", style="success")
    builder.button(text="➕ 90 дней", callback_data=f"admin_user:extend90:{user_id}", style="success")
    builder.button(text="✅ Активировать", callback_data=f"admin_user:activate:{user_id}", style="primary")
    builder.button(text="❌ Отключить", callback_data=f"admin_user:disable:{user_id}", style="danger")
    builder.button(text="🔄 Сбросить трафик", callback_data=f"admin_user:reset:{user_id}")
    builder.button(text="🔄 Обновить", callback_data=f"admin_user:refresh:{user_id}")
    builder.button(text="« Назад", callback_data="admin:panel")
    builder.adjust(1, 1, 2, 2, 2, 2, 2, 1)
    return builder.as_markup()


async def admin_rescue_panel() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Health check", callback_data="admin:health"))
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
    builder.row(InlineKeyboardButton(text="🚀 Отправить всем", callback_data="broadcast:send_all", style="success"))
    builder.row(InlineKeyboardButton(text="👥 Только активным", callback_data="broadcast:send_active"))
    if buttons:
        builder.row(InlineKeyboardButton(text="❌ Очистить кнопки", callback_data="broadcast:clear_buttons", style="danger"))
    builder.row(InlineKeyboardButton(text="« Отмена", callback_data="broadcast:cancel"))
    return builder.as_markup()


async def admins_list(admins: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for admin in admins:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ Удалить {admin['user_id']}",
                callback_data=f"admin:delete_admin:{admin['user_id']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="admin:panel"))
    return builder.as_markup()


async def sponsor_channels_list(channels: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin:add_channel", style="primary"))
    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {channel.get('title', channel['channel_ref'])}",
                callback_data=f"admin:delete_channel:{channel['channel_ref']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="admin:panel"))
    return builder.as_markup()


async def ads_list_keyboard(ads: Sequence[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить рекламу", callback_data="admin:add_ad", style="primary"))
    for ad in ads:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {ad['text']}",
                callback_data=f"admin:delete_ad:{ad['_id']}",
                style="danger",
            )
        )
    builder.row(InlineKeyboardButton(text="« Назад", callback_data="admin:panel"))
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