from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import settings
from core.app_core import MarzbanError, manager, safe_int_ts
from core.tariffs import calculate_tariffs, get_tariff_by_period
from database.db import db
from services.payments import payment_service
from utils import kbs
from uuid import uuid4
from utils.checksub import SubscriptionCheckResult, check_sub

logger = logging.getLogger(__name__)
router = Router()


class SupportRequest(StatesGroup):
    title = State()
    question = State()
    confirm = State()


class TopupState(StatesGroup):
    waiting_for_amount = State()


class CancelPaymentState(StatesGroup):
    waiting_for_reason = State()


async def _main_menu_text(user_tariff: str = "free", user_id: int | None = None) -> str:
    cfg = await db.get_app_config()
    project_name = cfg.get("project_name", settings.project_name)
    menu_desc = cfg.get("menu_description", settings.menu_description)
    img_part = f'<a href="{settings.menu_image_url}">&#8205;</a>' if settings.menu_image_url else ""

    return (
        f"{img_part}<b>{project_name}</b>\n\n"
        f"<blockquote>{menu_desc}</blockquote>\n\n"
        "Выберите нужный раздел в меню:"
    )


def _registration_text() -> str:
    lines = [
        "<b>Регистрация</b>",
        "",
        "Перед использованием бота необходимо принять условия сервиса.",
        "Ознакомьтесь с документами по кнопкам ниже и подтвердите согласие.",
    ]
    return "\n".join(lines)


_safe_int_ts = safe_int_ts


def _format_expire(timestamp: Any) -> str:
    ts = _safe_int_ts(timestamp)
    if not ts:
        return "Без срока"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _format_dt(dt: datetime | None) -> str:
    if not dt:
        return "n/a"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _normalize_dt(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _renewal_state(user_info: dict, local_user: dict | None) -> dict[str, object]:
    expire_ts = _safe_int_ts(user_info.get("expire"))
    if not expire_ts:
        return {"show_button": False, "can_renew": False, "reason": "У этой подписки нет срока окончания."}

    now = datetime.now(timezone.utc)
    expire_at = datetime.fromtimestamp(expire_ts, tz=timezone.utc)
    renew_window = timedelta(hours=settings.free_renew_window_hours)
    last_free_renew_at = _normalize_dt((local_user or {}).get("last_free_renew_at"))

    next_free_renew_at = None
    cooldown_ready = True
    if last_free_renew_at:
        next_free_renew_at = last_free_renew_at + timedelta(days=settings.free_renew_cooldown_days)
        cooldown_ready = now >= next_free_renew_at

    within_window = expire_at <= now + renew_window
    show_button = within_window and cooldown_ready

    if not within_window:
        reason = (
            "• <b>Бесплатное продление откроется за</b> "
            f"{settings.free_renew_window_hours} ч. до окончания подписки: "
            f"{_format_dt(expire_at - renew_window)}"
        )
    elif not cooldown_ready and next_free_renew_at:
        reason = (
            "• <b>Бесплатное продление доступно не чаще одного раза в</b> "
            f"{settings.free_renew_cooldown_days} дней. Следующая попытка: {_format_dt(next_free_renew_at)}"
        )
    else:
        reason = "• <b>Можно продлить подписку бесплатно</b> после повторной проверки каналов."

    return {
        "show_button": show_button,
        "can_renew": within_window and cooldown_ready,
        "reason": reason,
        "last_free_renew_at": last_free_renew_at,
    }


async def _animate_loading(message: Message, label: str) -> Message:
    status_message = await message.edit_text(f"⋯ {label}...")
    steps = ["◴", "◷", "◶", "◵"]
    for index, sym in enumerate(steps[:3], start=1):
        await status_message.edit_text(f"{sym} <b>{label}</b>{'.' * index}")
        await asyncio.sleep(0.12)
    return status_message


async def _show_sponsors(message: Message, result: SubscriptionCheckResult | None = None) -> None:
    channels = await db.list_channels()
    if not channels:
        user_tariff = await db.get_user_tariff(message.from_user.id)
        await message.answer(await _main_menu_text(user_tariff), reply_markup=await kbs.main_menu(user_tariff))
        return

    text = (
        "<b>◈ Доступ к бесплатному тарифу</b>\n\n"
        "Подпишитесь на каналы спонсоров для доступа к бесплатному VPN.\n"
        "Либо выберите <b>Премиум тариф</b> без рекламы и подписок!"
    )
    if result and result.unavailable_channels:
        text += "\n\n<i>Часть каналов недоступна для проверки и была пропущена.</i>"
    await message.answer(text, reply_markup=await kbs.sponsors_keyboard(channels))


async def _show_registration(message: Message) -> None:
    await message.answer(_registration_text(), reply_markup=await kbs.registration_keyboard())


async def _ensure_rules_accepted(message: Message) -> bool:
    local_user = await db.ensure_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if local_user.get("status") == "admin_blocked":
        reason = local_user.get("blocked_reason") or "no reason provided"
        await message.answer(f"Access blocked.\nReason: <code>{reason}</code>")
        return False
    if local_user.get("accepted_rules_at"):
        return True
    await _show_registration(message)
    return False


async def _ensure_access(message: Message) -> bool:
    if not await _ensure_rules_accepted(message):
        return False

    tariff = await db.get_user_tariff(message.from_user.id)
    if tariff == "premium":
        return True

    result = await check_sub(message.from_user.id, message)
    if not result.is_subscribed:
        await db.set_user_subscription_state(message.from_user.id, False)
        await _show_sponsors(message, result)
        return False

    await db.mark_user_registered(message.from_user.id, True)
    return True


async def _require_sponsors(call: CallbackQuery) -> bool:
    local_user = await db.ensure_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    if local_user.get("status") == "admin_blocked":
        reason = local_user.get("blocked_reason") or "no reason provided"
        await call.message.edit_text(f"Access blocked.\nReason: <code>{reason}</code>")
        await call.answer("Blocked", show_alert=True)
        return False
    if not local_user.get("accepted_rules_at"):
        await call.message.edit_text(_registration_text(), reply_markup=await kbs.registration_keyboard())
        await call.answer()
        return False

    tariff = await db.get_user_tariff(call.from_user.id)
    if tariff == "premium":
        return True

    result = await check_sub(call.from_user.id, call.message)
    if result.is_subscribed:
        await db.mark_user_registered(call.from_user.id, True)
        return True

    await db.set_user_subscription_state(call.from_user.id, False)
    await call.message.edit_text(
        "<b>◈ Доступ закрыт</b>\n\nПодпишитесь на каналы спонсоров для доступа к бесплатному VPN.",
        reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
    )
    await call.answer()
    return False


async def _load_subscription_context(user_id: int) -> tuple[dict, dict, dict[str, object], str]:
    tariff = await db.get_user_tariff(user_id)
    local_user = await db.get_user_by_id(user_id)

    data_limit = settings.trial_limit_gb if tariff == "free" else 0
    expire_days = settings.trial_days if tariff == "free" else 30
    username = db.build_vpn_username(user_id)

    try:
        user_info = await manager.ensure_subscription_user(
            username=username,
            data_limit_gb=data_limit,
            expire_days=expire_days,
            note=f"Telegram user {user_id} ({tariff})",
            tariff=tariff,
        )
    except MarzbanError as exc:
        if exc.status_code == 401 or "incorrect username or password" in exc.detail.lower():
            logger.warning("PasarGuard 401 [Incorrect username or password] for user %s: recreating user in database", user_id)
            try:
                await manager.delete_user(username)
            except Exception:
                pass
            await db.delete_user(user_id)
            local_user = await db.ensure_user(user_id, None, None)
            try:
                user_info = await manager.ensure_subscription_user(
                    username=username,
                    data_limit_gb=data_limit,
                    expire_days=expire_days,
                    note=f"Telegram user {user_id} ({tariff})",
                    tariff=tariff,
                )
            except Exception as retry_exc:
                logger.error("Could not ensure PasarGuard user %s after DB recreate: %s", username, retry_exc)
                now_ts = int(datetime.now(timezone.utc).timestamp())
                exp_ts = now_ts + (expire_days * 86400)
                user_info = {
                    "username": username,
                    "status": "active",
                    "data_limit": data_limit * (1024**3),
                    "used_traffic": 0,
                    "expire": exp_ts,
                    "subscription_url": f"{settings.marzban_url.rstrip('/')}/sub/{username}",
                }
        else:
            raise

    return local_user or {}, user_info, _renewal_state(user_info, local_user), tariff


def _subscription_text(user_info: dict, user_id: int, renewal: dict[str, object], tariff: str) -> str:
    status_map = {
        "active": "Активна",
        "expired": "Истекла",
        "limited": "Лимит исчерпан",
        "disabled": "Отключена",
        "on_hold": "На паузе",
    }
    limit_value = user_info.get("data_limit", 0)
    used = manager.format_bytes(user_info.get("used_traffic", 0))
    limit = manager.format_bytes(limit_value) if limit_value else "Безлимит"
    tariff_title = "✦ Премиум [LTE]" if tariff == "premium" else "Бесплатный"

    lines = [
        "<b>◈ Ваша подписка</b>",
        "",
        f"<blockquote>ID: <code>{user_id}</code>",
        f"Тариф: <b>{tariff_title}</b>",
        f"Статус: <b>{status_map.get(user_info.get('status'), user_info.get('status', 'unknown'))}</b>",
        f"Трафик: <code>{used} / {limit}</code>",
        f"Действует до: <code>{_format_expire(user_info.get('expire'))}</code></blockquote>",
    ]

    if tariff == "free":
        lines.extend(["", str(renewal["reason"])])
        if renewal.get("last_free_renew_at"):
            lines.append(f"• Последнее бесплатное продление: <code>{_format_dt(renewal['last_free_renew_at'])}</code>")
    else:
        lines.extend(["", "✦ Премиум-подключение активно на выделенных серверах (LTE-обходы)."])

    return "\n".join(lines)


# ---------------- Start and Main Menu ----------------
@router.message(CommandStart())
@router.message(Command("menu"))
async def start_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _ensure_access(message):
        return

    tariff = await db.get_user_tariff(message.from_user.id)
    await message.answer(await _main_menu_text(tariff, message.from_user.id), reply_markup=await kbs.main_menu(tariff))


@router.callback_query(F.data == "user:menu")
async def menu_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    tariff = await db.get_user_tariff(call.from_user.id)
    await call.message.edit_text(await _main_menu_text(tariff, call.from_user.id), reply_markup=await kbs.main_menu(tariff))
    await call.answer()


# ---------------- Connection & Subscription ----------------
@router.callback_query(F.data.in_({"user:connect", "user:sub", "my_sub", "user:connect:free", "user:select_free"}))
async def connect_page(call: CallbackQuery) -> None:
    """Единый раздел подключения и информации о подписке."""
    if not await _require_sponsors(call):
        return

    tariff = await db.get_user_tariff(call.from_user.id)

    # Для бесплатного тарифа показываем рекламу 3 секунды перед переходом к подписке
    if tariff == "free":
        ad = await db.get_random_ad()
        if ad and ad.get("text") and ad.get("url"):
            ad_text = (
                "<b>◈ Реклама от спонсора:</b>\n"
                f"<blockquote><b>{ad['text']}</b>\n"
                f"➤ <a href='{ad['url']}'>Перейти на сайт спонсора</a></blockquote>\n\n"
                "⋯ <i>Загрузка данных вашей подписки (3 сек)...</i>"
            )
            try:
                await call.message.edit_text(
                    ad_text,
                    reply_markup=await kbs.ad_interstitial_keyboard(ad["url"], ad["text"]),
                )
                await asyncio.sleep(3)
            except Exception:
                pass

    try:
        local_user, user_info, renewal, tariff = await _load_subscription_context(call.from_user.id)
    except MarzbanError as exc:
        await call.message.edit_text(f"Не удалось получить конфигурацию: {exc.detail}", reply_markup=await kbs.profile_keyboard())
        await call.answer()
        return

    subscription_url = user_info.get("subscription_url", "")
    status_map = {
        "active": "Активна",
        "expired": "Истекла",
        "limited": "Лимит исчерпан",
        "disabled": "Отключена",
        "on_hold": "На паузе",
    }
    limit_value = user_info.get("data_limit", 0)
    used = manager.format_bytes(user_info.get("used_traffic", 0))
    limit = manager.format_bytes(limit_value) if limit_value else "Безлимит"
    status_str = status_map.get(user_info.get("status"), user_info.get("status", "unknown"))

    if tariff == "premium":
        auto_renew = await db.get_user_auto_renew(call.from_user.id)
        text = (
            "<b>◈ Подключение к VPN</b>\n\n"
            f"<blockquote>ID: <code>{call.from_user.id}</code>\n"
            "Тариф: <b>✦ Премиум [LTE]</b> (серверы <b>aska_premium</b>)\n"
            "Связь: <b>LTE-обходы активны</b> (МТС, МегаФон, Билайн, Tele2, Yota)\n"
            f"Статус: <b>{status_str}</b>\n"
            f"Трафик: <code>{used} / Безлимит</code>\n"
            f"Действует до: <code>{_format_expire(user_info.get('expire'))}</code></blockquote>\n\n"
            "✦ Премиум-подключение активно на выделенных скоростных серверах без рекламы.\n\n"
            "Нажмите <b>«Открыть подписку»</b> для импорта настроек в VPN-приложение (Happ, v2rayNG, Streisand, Nekobox):"
        )
        await call.message.edit_text(
            text,
            reply_markup=await kbs.premium_connect_keyboard(
                subscription_url=subscription_url,
                auto_renew=auto_renew,
            ),
        )
    else:
        text = (
            "<b>◈ Подключение к VPN</b>\n\n"
            f"<blockquote>ID: <code>{call.from_user.id}</code>\n"
            "Тариф: <b>Бесплатный</b> (серверы <b>aska_free</b>)\n"
            f"Статус: <b>{status_str}</b>\n"
            f"Трафик: <code>{used} / {limit}</code>\n"
            f"Действует до: <code>{_format_expire(user_info.get('expire'))}</code></blockquote>\n\n"
            f"{renewal['reason']}\n\n"
            "Нажмите <b>«Открыть подписку»</b> для импорта настроек в VPN-приложение (Happ, v2rayNG, Streisand, Nekobox):"
        )
        await call.message.edit_text(
            text,
            reply_markup=await kbs.free_connect_keyboard(
                subscription_url=subscription_url,
                show_free_renew=bool(renewal["show_button"]),
            ),
        )
    await call.answer()


@router.callback_query(F.data == "user:connect:premium")
async def connect_premium_page(call: CallbackQuery) -> None:
    """Подключение по тарифу Премиум."""
    tariff = await db.get_user_tariff(call.from_user.id)
    if tariff != "premium":
        await tariffs_page(call)
        return
    await connect_page(call)


# ---------------- Subscription Settings ----------------
@router.callback_query(F.data == "user:sub_settings")
async def sub_settings_page(call: CallbackQuery) -> None:
    tariff = await db.get_user_tariff(call.from_user.id)
    username = db.build_vpn_username(call.from_user.id)
    hwids = await manager.get_user_hwids(username)
    device_limit = await db.get_user_device_limit(call.from_user.id)
    count = len(hwids)
    is_premium = (tariff == "premium")
    auto_renew = await db.get_user_auto_renew(call.from_user.id) if is_premium else False

    tariff_label = "💎 Премиум [LTE]" if is_premium else "🆓 Бесплатный"
    auto_renew_line = f"🔄 Автопродление: <b>{'Включено' if auto_renew else 'Отключено'}</b>\n" if is_premium else ""

    text = (
        "⚙️ <b>Настройки подписки</b>\n\n"
        f"<blockquote>⭐️ Тариф: <b>{tariff_label}</b>\n"
        f"📱 Устройства: <b>{count} из {device_limit}</b>\n"
        f"{auto_renew_line}"
        "🔗 Ссылка: <b>Активна</b></blockquote>\n\n"
        "Управляйте привязанными устройствами, ссылкой подписки и параметрами продления:"
    )
    await call.message.edit_text(
        text,
        reply_markup=await kbs.sub_settings_keyboard(
            is_premium=is_premium,
            auto_renew=auto_renew,
            devices_count=count,
            device_limit=device_limit,
        ),
    )
    await call.answer()


# ---------------- Tariffs & Payment ----------------
@router.callback_query(F.data == "user:tariffs")
async def tariffs_page(call: CallbackQuery) -> None:
    cfg = await db.get_app_config()
    base_price = cfg["premium_base_price_rub"]
    discount_base = cfg["default_discount_percent"]
    options = calculate_tariffs(base_price, discount_base)
    active_tariff = await db.get_user_tariff(call.from_user.id)
    balance = await db.get_user_balance(call.from_user.id)
    can_overdraft, _ = await db.can_use_overdraft(call.from_user.id)

    lines = [
        "<b>✦ Тариф Премиум (с технологией LTE-обходов)</b>",
        "",
        f"Баланс: <b>{balance:.2f} ₽</b>",
        "",
        "<blockquote>• <b>LTE-обходы:</b> стабильная связь на мобильных операторах (МТС, МегаФон, Билайн, Tele2, Yota) без просадок скорости и блокировок\n"
        "• Выделенные высокоскоростные серверы группы <b>aska_premium</b>\n"
        "• Полное отсутствие рекламы и каналов-спонсоров\n"
        "• Полный безлимит трафика на максимальной скорости</blockquote>",
        "",
        "<b>Скидки при выборе длительного периода:</b>",
    ]

    for opt in options:
        if opt.discount_percent > 0:
            lines.append(f"• <b>{opt.title}:</b> <code>{opt.total_price} ₽</code> (-{int(opt.discount_percent)}%, выгода {opt.savings} ₽)")
        else:
            lines.append(f"• <b>{opt.title}:</b> <code>{opt.total_price} ₽</code> ({opt.price_per_month} ₽/мес)")

    lines.append("\nВыберите подходящий вариант ниже:")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.tariffs_keyboard(options, active_tariff, can_overdraft=can_overdraft),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pay:select:"))
async def select_tariff_payment(call: CallbackQuery) -> None:
    """Выбор способа оплаты тарифа (с баланса или через RollyPay)."""
    period_id = call.data.split(":", 2)[2]
    cfg = await db.get_app_config()
    base_price = cfg["premium_base_price_rub"]
    discount_base = cfg["default_discount_percent"]
    option = get_tariff_by_period(period_id, base_price, discount_base)

    if not option:
        await call.answer("Тариф не найден", show_alert=True)
        return

    balance = await db.get_user_balance(call.from_user.id)
    can_pay_balance = (balance >= option.total_price)

    lines = [
        f"<b>✦ Тариф: {option.title}</b>",
        "",
        f"Срок действия: <b>{option.days} дней</b>",
        f"Стоимость: <b>{option.total_price} ₽</b>",
        f"Баланс: <b>{balance:.2f} ₽</b>",
    ]
    if option.discount_percent > 0:
        lines.append(f"Скидка: <b>-{int(option.discount_percent)}%</b> (выгода {option.savings} ₽)")

    lines.extend([
        "",
        "Выберите способ оплаты ниже:",
    ])

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.tariff_pay_options_keyboard(
            period_id=period_id,
            price=option.total_price,
            can_pay_balance=can_pay_balance,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pay:balance:"))
async def pay_from_balance(call: CallbackQuery) -> None:
    """Мгновенная оплата тарифа с баланса пользователя."""
    period_id = call.data.split(":", 2)[2]
    cfg = await db.get_app_config()
    base_price = cfg["premium_base_price_rub"]
    discount_base = cfg["default_discount_percent"]
    option = get_tariff_by_period(period_id, base_price, discount_base)

    if not option:
        await call.answer("Тариф не найден", show_alert=True)
        return

    balance = await db.get_user_balance(call.from_user.id)
    if balance < option.total_price:
        await call.answer("Недостаточно средств на балансе. Пополните баланс.", show_alert=True)
        return

    # Списываем средства
    new_balance = await db.change_user_balance(call.from_user.id, -option.total_price)
    now = datetime.now(timezone.utc)
    user = await db.get_user_by_id(call.from_user.id)
    cur_exp = user.get("premium_expires_at") if user else None
    if cur_exp:
        if cur_exp.tzinfo is None:
            cur_exp = cur_exp.replace(tzinfo=timezone.utc)
        base_dt = max(cur_exp, now)
    else:
        base_dt = now

    new_expire = base_dt + timedelta(days=option.days)
    await db.set_user_tariff(call.from_user.id, "premium", new_expire)

    order_id = f"bal_{uuid4().hex[:12]}"
    await db.create_payment_order(
        user_id=call.from_user.id,
        order_id=order_id,
        amount=option.total_price,
        days=option.days,
        months=option.months,
        pay_url="",
        payment_id=order_id,
        status="paid",
        order_type="balance_tariff",
    )

    username = db.build_vpn_username(call.from_user.id)
    try:
        await manager.upgrade_user_to_premium(username, option.days)
    except Exception as exc:
        logger.exception("Failed to upgrade user %s in PasarGuard: %s", username, exc)

    text = (
        "<b>✓ Тариф Премиум успешно оплачен с баланса</b>\n\n"
        f"Тариф: <b>{option.title} ({option.days} дней)</b>\n"
        f"Списано с баланса: <b>{option.total_price} ₽</b>\n"
        f"Остаток на балансе: <b>{new_balance:.2f} ₽</b>\n"
        f"Действует до: <code>{new_expire.strftime('%d.%m.%Y %H:%M UTC')}</code>\n\n"
        "✦ Серверы <b>aska_premium</b> активны без рекламы."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➤ Подключить Премиум", callback_data="user:connect:premium", style="success"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    await call.message.edit_text(text, reply_markup=builder.as_markup())
    await call.answer("Оплата с баланса успешна!")


# ---------------- Overdraft (Перерасход / Доверенный платеж) ----------------
@router.callback_query(F.data == "user:overdraft")
async def overdraft_handler(call: CallbackQuery) -> None:
    """
    Перерасход: дает 3 дня премиума по спеццене,
    которая уведет баланс в минус (аналог доверенного платежа).
    Только один раз и при положительном балансе.
    """
    can_use, reason = await db.can_use_overdraft(call.from_user.id)
    if not can_use:
        await call.answer(reason, show_alert=True)
        return

    price = settings.overdraft_price_rub
    days = 3
    await db.apply_overdraft(call.from_user.id, price=price, days=days)
    new_balance = await db.get_user_balance(call.from_user.id)

    username = db.build_vpn_username(call.from_user.id)
    try:
        await manager.upgrade_user_to_premium(username, days=days)
    except Exception as exc:
        logger.warning("Failed to upgrade PasarGuard user %s for overdraft: %s", username, exc)

    now = datetime.now(timezone.utc)
    user = await db.get_user_by_id(call.from_user.id)
    expire_dt = user.get("premium_expires_at") if user else (now + timedelta(days=days))

    text = (
        "<b>✦ Услуга «Перерасход» активирована</b>\n\n"
        f"Вам начислено <b>{days} дня Премиума</b>.\n"
        f"Специальная стоимость: <b>{price:.2f} ₽</b>\n"
        f"Текущий баланс: <b>{new_balance:.2f} ₽</b> (ушёл в минус)\n"
        f"Премиум действует до: <code>{_format_dt(_normalize_dt(expire_dt))}</code>\n\n"
        "Вы переведены в группу серверов <b>aska_premium</b>.\n"
        "Пожалуйста, пополните баланс в течение 3 дней, чтобы подписка не была заблокирована."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➤ Подключить Премиум", callback_data="user:connect:premium", style="success"))
    builder.row(InlineKeyboardButton(text="[+] Пополнить баланс", callback_data="user:topup", style="primary"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
    await call.message.edit_text(text, reply_markup=builder.as_markup())
    await call.answer("Перерасход успешно активирован!")


# ---------------- Topup (Пополнение баланса) ----------------
@router.callback_query(F.data == "user:topup")
async def topup_page(call: CallbackQuery) -> None:
    balance = await db.get_user_balance(call.from_user.id)
    text = (
        "<b>◈ Пополнение баланса</b>\n\n"
        f"Ваш текущий баланс: <b>{balance:.2f} ₽</b>\n\n"
        "Выберите сумму для пополнения через СБП / банковскую карту:"
    )
    await call.message.edit_text(text, reply_markup=await kbs.topup_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("user:topup_amount:"))
async def topup_amount_select(call: CallbackQuery) -> None:
    amount = float(call.data.split(":", 2)[2])
    await call.message.edit_text("⋯ Формирую счет на пополнение...")
    try:
        payment = await payment_service.create_topup_payment(
            user_id=call.from_user.id,
            amount=amount,
        )
    except Exception as exc:
        await call.message.edit_text(
            f"✕ Ошибка создания платежа: <code>{exc}</code>",
            reply_markup=await kbs.profile_keyboard(),
        )
        await call.answer()
        return

    order_id = payment["order_id"]
    pay_url = payment.get("pay_url", "")
    lines = [
        "<b>◈ Пополнение баланса</b>",
        "",
        f"Сумма к пополнению: <b>{amount:.2f} ₽</b>",
        "",
        "<blockquote>После нажатия кнопки «Оплатить» вы перейдете на страницу оплаты RollyPay (СБП, банковская карта).</blockquote>",
        "",
        "После оплаты нажмите <b>«Проверить оплату»</b> для зачисления средств:",
    ]
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.payment_keyboard(
            pay_url,
            order_id,
            amount,
            back_callback="user:topup",
            back_text="« Назад к пополнению",
        ),
    )
    await call.answer()


@router.callback_query(F.data == "user:topup_custom")
async def topup_custom_prompt(call: CallbackQuery, state: FSMContext) -> None:
    """Запрос ввода произвольной суммы для пополнения баланса."""
    await state.set_state(TopupState.waiting_for_amount)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="« Назад к пополнению", callback_data="user:topup"))
    await call.message.edit_text(
        "<b>◈ Пополнение баланса на свою сумму</b>\n\n"
        "Введите желаемую сумму пополнения в рублях (от 10 до 50 000 ₽):\n\n"
        "<i>Например: 250</i>",
        reply_markup=builder.as_markup(),
    )
    await call.answer()


@router.message(TopupState.waiting_for_amount)
async def topup_custom_amount_input(message: Message, state: FSMContext) -> None:
    """Обработка введенной пользователем суммы пополнения."""
    text = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="« Назад к пополнению", callback_data="user:topup"))
        await message.answer(
            "• Пожалуйста, введите сумму числом (от 10 до 50 000 ₽).\n\nНапример: <code>250</code>",
            reply_markup=builder.as_markup(),
        )
        return

    if amount < 10 or amount > 50000:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="« Назад к пополнению", callback_data="user:topup"))
        await message.answer(
            "• Сумма пополнения должна быть от <b>10</b> до <b>50 000</b> ₽.",
            reply_markup=builder.as_markup(),
        )
        return

    await state.clear()
    try:
        payment = await payment_service.create_topup_payment(
            user_id=message.from_user.id,
            amount=amount,
        )
    except Exception as exc:
        await message.answer(
            f"✕ Ошибка создания платежа: <code>{exc}</code>",
            reply_markup=await kbs.profile_keyboard(),
        )
        return

    order_id = payment["order_id"]
    pay_url = payment.get("pay_url", "")
    lines = [
        "<b>◈ Пополнение баланса</b>",
        "",
        f"Сумма к пополнению: <b>{amount:.2f} ₽</b>",
        "",
        "<blockquote>После нажатия кнопки «Оплатить» вы перейдете на страницу оплаты RollyPay (СБП, банковская карта).</blockquote>",
        "",
        "После оплаты нажмите <b>«Проверить оплату»</b> для зачисления средств:",
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=await kbs.payment_keyboard(
            pay_url,
            order_id,
            amount,
            back_callback="user:topup",
            back_text="« Назад к пополнению",
        ),
    )


# ---------------- Payment History & Cancellation ----------------
@router.callback_query(F.data.startswith("user:history"))
async def payment_history_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    parts = call.data.split(":")
    page = 1
    if len(parts) >= 3:
        try:
            page = max(1, int(parts[2]))
        except ValueError:
            page = 1

    page_size = 5
    total_count = await db.count_user_payments(call.from_user.id)
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    payments = await db.get_user_payments(call.from_user.id, limit=page_size, offset=offset)

    if not payments and page == 1:
        lines = [
            "📜 <b>История пополнений и платежей</b>\n",
            "<blockquote>У вас пока нет сохраненных операций пополнения.</blockquote>\n",
            "Вы можете пополнить баланс в любой момент:",
        ]
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="user:topup", style="primary"))
        builder.row(InlineKeyboardButton(text="👤 Личный кабинет", callback_data="user:profile"))
        builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))
        await call.message.edit_text("\n".join(lines), reply_markup=builder.as_markup())
        await call.answer()
        return

    status_map = {
        "paid": "✓ Зачислено",
        "refund_requested": "⋯ Запрошена отмена",
        "refunded": "↺ Возврат одобрен",
        "refund_declined": "✕ В отмене отказано",
        "created": "⋯ Ожидает оплаты",
        "canceled": "✕ Отменен",
        "expired": "✕ Истек",
    }
    lines = [
        "📜 <b>История пополнений и платежей</b>",
        f"<i>Страница {page} из {total_pages} (всего записей: {total_count})</i>\n",
        "<blockquote>",
    ]
    for p in payments:
        dt_str = _format_dt(_normalize_dt(p.get("created_at")))
        amount = p.get("amount", 0.0)
        st = status_map.get(p.get("status", ""), p.get("status", "unknown"))
        order_id = p.get("order_id", "")
        order_type = p.get("order_type", "topup")
        type_label = "Пополнение" if order_type == "topup" else "Тариф"
        lines.append(f"• <b>{amount:.2f} ₽</b> | {type_label} | {st}\n  <code>{order_id}</code> ({dt_str})")
    lines.extend([
        "</blockquote>\n",
        "Выберите операцию ниже для деталей или запроса отмены:",
    ])
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.payment_history_keyboard(payments, page=page, total_pages=total_pages),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("user:order:"))
async def order_details_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order or order.get("user_id") != call.from_user.id:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    status_map = {
        "paid": "✓ Оплачен",
        "refund_requested": "⋯ Запрошена отмена (на проверке у админа)",
        "refunded": "↺ Возврат одобрен",
        "refund_declined": "✕ В отмене отказано",
        "created": "⋯ Ожидает оплаты",
        "canceled": "✕ Отменен",
        "expired": "✕ Истек",
    }
    type_map = {
        "topup": "Пополнение баланса",
        "tariff": f"Оплата тарифа ({order.get('days', 30)} дн.)",
        "balance_tariff": f"Оплата тарифа с баланса ({order.get('days', 30)} дн.)",
    }
    status_str = status_map.get(order.get("status"), order.get("status", "unknown"))
    type_str = type_map.get(order.get("order_type"), order.get("order_type", "Платеж"))
    created_str = _format_dt(_normalize_dt(order.get("created_at")))

    lines = [
        "<b>◈ Детали платежа</b>",
        "",
        f"<blockquote>Заказ: <code>{order_id}</code>\n"
        f"Тип: <b>{type_str}</b>\n"
        f"Сумма: <b>{order.get('amount', 0):.2f} ₽</b>\n"
        f"Дата создания: <code>{created_str}</code>\n"
        f"Статус: <b>{status_str}</b>",
    ]
    if order.get("paid_at"):
        lines.append(f"Дата оплаты: <code>{_format_dt(_normalize_dt(order['paid_at']))}</code>")
    if order.get("refund_reason"):
        lines.append(f"Причина отмены: <i>{order['refund_reason']}</i>")
    lines.append("</blockquote>")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.order_details_keyboard(order),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pay:cancel_req:"))
async def request_cancel_payment_prompt(call: CallbackQuery, state: FSMContext) -> None:
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order or order.get("user_id") != call.from_user.id:
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if order.get("status") != "paid":
        await call.answer("Отмена возможна только для оплаченных заказов.", show_alert=True)
        return

    await state.set_state(CancelPaymentState.waiting_for_reason)
    await state.update_data(order_id=order_id)
    text = (
        "<b>↺ Запрос на отмену платежа</b>\n\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Сумма: <b>{order.get('amount', 0):.2f} ₽</b>\n\n"
        "Напишите причину отмены ответным сообщением или нажмите «Отправить без описания»:"
    )
    await call.message.edit_text(text, reply_markup=await kbs.cancel_request_keyboard(order_id))
    await call.answer()


@router.callback_query(F.data.startswith("pay:cancel_submit:"))
async def request_cancel_payment_skip(call: CallbackQuery, state: FSMContext) -> None:
    order_id = call.data.split(":", 2)[2]
    await state.clear()
    await _process_cancel_request(call.message, call.from_user.id, order_id, reason="Не указана", is_callback=True, call=call)


@router.message(CancelPaymentState.waiting_for_reason)
async def request_cancel_payment_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    await state.clear()
    if not order_id:
        await message.answer("Ошибка: идентификатор заказа утерян.")
        return
    reason = (message.text or "").strip() or "Не указана"
    await _process_cancel_request(message, message.from_user.id, order_id, reason=reason, is_callback=False)


async def _process_cancel_request(
    target_message: Message,
    user_id: int,
    order_id: str,
    reason: str,
    is_callback: bool = False,
    call: CallbackQuery | None = None,
) -> None:
    order = await db.get_payment_order(order_id)
    if not order or order.get("user_id") != user_id:
        if call:
            await call.answer("Заказ не найден.", show_alert=True)
        else:
            await target_message.answer("Заказ не найден.")
        return

    if order.get("status") != "paid":
        msg = "Отмена возможна только для оплаченных заказов."
        if call:
            await call.answer(msg, show_alert=True)
        else:
            await target_message.answer(msg)
        return

    updated = await db.request_payment_refund(order_id=order_id, user_id=user_id, reason=reason)
    if not updated:
        msg = "Не удалось отправить запрос или он уже был отправлен."
        if call:
            await call.answer(msg, show_alert=True)
        else:
            await target_message.answer(msg)
        return

    user_balance = await db.get_user_balance(user_id)
    local_user = await db.get_user_by_id(user_id)
    user_tag = f"@{local_user.get('username')}" if local_user and local_user.get("username") else f"ID {user_id}"

    # Notify admins
    admin_text = (
        "<b>[!] Запрос отмены платежа</b>\n\n"
        f"<blockquote>Пользователь: <code>{user_id}</code> ({user_tag})\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Сумма: <b>{order.get('amount', 0):.2f} ₽</b>\n"
        f"Тип: <b>{order.get('order_type', 'topup')}</b>\n"
        f"Баланс пользователя: <b>{user_balance:.2f} ₽</b>\n"
        f"Причина: <i>{reason}</i></blockquote>\n\n"
        "Выберите решение:"
    )
    recipients = await db.list_support_recipient_ids()
    for rec_id in recipients:
        try:
            await target_message.bot.send_message(
                rec_id,
                admin_text,
                reply_markup=await kbs.admin_refund_action_keyboard(order_id),
            )
        except Exception:
            pass

    confirm_text = (
        "<b>✓ Запрос на отмену отправлен на проверку</b>\n\n"
        f"<blockquote>Заказ: <code>{order_id}</code>\n"
        f"Сумма: <b>{order.get('amount', 0):.2f} ₽</b>\n"
        f"Причина: <i>{reason}</i></blockquote>\n\n"
        "Администратор рассмотрит заявку. Уведомление с решением поступит в этот чат."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="• К истории пополнений", callback_data="user:history"))
    builder.row(InlineKeyboardButton(text="◈ Личный кабинет", callback_data="user:profile"))

    if is_callback and call:
        await call.message.edit_text(confirm_text, reply_markup=builder.as_markup())
        await call.answer("Запрос отправлен")
    else:
        await target_message.answer(confirm_text, reply_markup=builder.as_markup())


# ---------------- Auto-Renewal Toggle ----------------
@router.callback_query(F.data == "user:toggle_autorenew")
async def toggle_autorenew(call: CallbackQuery) -> None:
    current = await db.get_user_auto_renew(call.from_user.id)
    new_state = not current
    await db.set_user_auto_renew(call.from_user.id, new_state)
    await sub_settings_page(call)


@router.callback_query(F.data.startswith("pay:start:"))
async def start_payment(call: CallbackQuery) -> None:
    period_id = call.data.split(":", 2)[2]
    cfg = await db.get_app_config()
    base_price = cfg["premium_base_price_rub"]
    discount_base = cfg["default_discount_percent"]
    option = get_tariff_by_period(period_id, base_price, discount_base)

    if not option:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await call.message.edit_text("⋯ Формирую счет на оплату...")
    try:
        payment = await payment_service.create_payment(
            user_id=call.from_user.id,
            amount=option.total_price,
            days=option.days,
            months=option.months,
            description=f"Премиум VPN ({option.title})",
        )
    except Exception as exc:
        await call.message.edit_text(
            f"✕ Ошибка создания платежа: <code>{exc}</code>\nПопробуйте позже или обратитесь в поддержку.",
            reply_markup=await kbs.profile_keyboard(),
        )
        await call.answer()
        return

    order_id = payment["order_id"]
    pay_url = payment.get("pay_url", "")

    lines = [
        "<b>✦ Оплата тарифа Премиум</b>",
        "",
        f"Тариф: <b>{option.title}</b>",
        f"Срок действия: <b>{option.days} дней</b>",
        f"Сумма к оплате: <b>{option.total_price} ₽</b>",
    ]
    if option.discount_percent > 0:
        lines.append(f"Ваша скидка: <b>-{int(option.discount_percent)}%</b> (экономия {option.savings} ₽)")

    lines.extend(
        [
            "",
            "<blockquote>После нажатия кнопки «Оплатить» вы перейдете на защищенную страницу RollyPay (СБП, банковская карта).</blockquote>",
            "",
            "После оплаты нажмите <b>«Проверить оплату»</b> для активации:",
        ]
    )

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.payment_keyboard(pay_url, order_id, option.total_price),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pay:check:"))
async def check_payment_status(call: CallbackQuery) -> None:
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order:
        await call.answer("Счет не найден", show_alert=True)
        return

    payment_id = order.get("payment_id")
    if not payment_id:
        await call.answer("Идентификатор платежа отсутствует", show_alert=True)
        return

    try:
        data = await payment_service.get_payment(payment_id)
        status = data.get("status", "created")
    except Exception as exc:
        await call.answer(f"Ошибка проверки: {exc}", show_alert=True)
        return

    if status == "paid":
        await payment_service.process_payment_success(order_id, bot=call.bot)
        if order.get("order_type") == "topup":
            await call.answer("✓ Баланс успешно пополнен!", show_alert=True)
            await profile_page(call)
        else:
            await call.answer("✓ Оплата успешно подтверждена! Премиум активирован.", show_alert=True)
            await connect_page(call)
        return

    if status in {"canceled", "expired"}:
        await db.update_payment_status(order_id, status)
        msg = "Срок действия этого счета истек." if order.get("order_type") == "topup" else "Срок действия этого счета истек. Создайте новый в разделе Тарифы."
        await call.answer(msg, show_alert=True)
        return

    # In sandbox/demo mode when ROLLY_API_KEY is not set, allow admin/user test confirmation
    if order.get("payment_id", "").startswith("pay_mock_"):
        await payment_service.process_payment_success(order_id, bot=call.bot)
        if order.get("order_type") == "topup":
            await call.answer("✓ [Тестовый режим] Баланс успешно пополнен!", show_alert=True)
            await profile_page(call)
        else:
            await call.answer("✓ [Тестовый режим] Оплата успешно подтверждена!", show_alert=True)
            await connect_page(call)
        return

    await call.answer("⋯ Оплата еще не поступила. Повторите попытку через 10-30 секунд.", show_alert=True)


# ---------------- Subscriptions ----------------
subscription_page = connect_page


@router.callback_query(F.data == "user:renew_free")
async def renew_free_subscription(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    try:
        local_user, user_info, renewal, tariff = await _load_subscription_context(call.from_user.id)
    except MarzbanError as exc:
        await call.answer(f"Ошибка: {exc.detail}", show_alert=True)
        return

    if not renewal["can_renew"]:
        await call.answer(str(renewal["reason"]), show_alert=True)
        return

    await _animate_loading(call.message, "Продление")
    try:
        user_info = await manager.extend_user_subscription(db.build_vpn_username(call.from_user.id), settings.trial_days)
    except MarzbanError as exc:
        await call.message.edit_text(f"Не удалось продлить подписку: {exc.detail}", reply_markup=await kbs.profile_keyboard())
        await call.answer()
        return

    await db.mark_free_renewal(call.from_user.id)
    await call.answer("Подписка успешно продлена!")
    await connect_page(call)


# ---------------- Profile & FAQ ----------------
@router.callback_query(F.data == "user:profile")
async def profile_page(call: CallbackQuery) -> None:
    local_user = await db.get_user_by_id(call.from_user.id)
    if not local_user:
        await db.ensure_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
        local_user = await db.get_user_by_id(call.from_user.id)

    tariff = await db.get_user_tariff(call.from_user.id)
    balance = await db.get_user_balance(call.from_user.id)
    can_overdraft, _ = await db.can_use_overdraft(call.from_user.id)
    overdraft_used = bool(local_user.get("overdraft_used", False))

    if tariff == "premium":
        tariff_label = "💎 Премиум [LTE]"
        expire_dt = local_user.get("premium_expires_at")
        expire_val = _format_dt(_normalize_dt(expire_dt)) if expire_dt else "Активен"
    else:
        tariff_label = "🆓 Бесплатный"
        expire_val = "Активен"

    if overdraft_used:
        overdraft_str = "использован"
    elif can_overdraft:
        overdraft_str = "доступен (+3 дня)"
    else:
        overdraft_str = "недоступен"

    text = (
        "👤 <b>Личный кабинет</b>\n\n"
        f"<blockquote>🆔 ID: <code>{local_user['user_id']}</code>\n"
        f"💳 Баланс: <b>{balance:.2f} ₽</b>\n"
        f"⭐️ Тариф: <b>{tariff_label}</b>\n"
        f"⏳ Срок: <code>{expire_val}</code>\n"
        f"⚡️ Перерасход: <b>{overdraft_str}</b></blockquote>"
    )

    await call.message.edit_text(
        text,
        reply_markup=await kbs.profile_keyboard(can_overdraft=can_overdraft),
    )
    await call.answer()


# ---------------- Device Management ----------------
@router.callback_query(F.data == "user:devices")
async def user_devices_page(call: CallbackQuery) -> None:
    username = db.build_vpn_username(call.from_user.id)
    hwids = await manager.get_user_hwids(username)
    device_limit = await db.get_user_device_limit(call.from_user.id)
    count = len(hwids)
    tariff = await db.get_user_tariff(call.from_user.id)

    lines = [
        "📱 <b>Управление устройствами</b>",
        "",
        f"Лимит по вашему тарифу: <b>{count} из {device_limit}</b>",
    ]

    if count >= device_limit:
        tariff_hint = "перейдите на 💎 Премиум" if tariff == "free" else "обратитесь к администратору"
        lines.extend([
            "",
            "⚠️ <b>Внимание: достигнут лимит устройств!</b>",
            f"<i>Новые устройства не смогут подключиться к VPN. Чтобы подключить новое устройство, удалите одно из текущих ниже или {tariff_hint}.</i>",
        ])

    lines.append("")
    if not hwids:
        lines.extend([
            "<blockquote>У вас пока нет привязанных устройств.</blockquote>",
            "",
            "Устройства регистрируются автоматически при первом подключении через VPN-приложение (Happ, v2rayNG, Streisand, FoXray и др.).",
        ])
    else:
        lines.append("Нажмите на устройство ниже для просмотра информации или удаления:")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.devices_keyboard(hwids, device_limit),
    )
    await call.answer()


@router.callback_query(F.data.startswith("user:dev:") & ~F.data.startswith("user:dev_del") & ~F.data.startswith("user:dev_reset"))
async def device_details_page(call: CallbackQuery) -> None:
    short_hwid = call.data.split(":", 2)[2]
    username = db.build_vpn_username(call.from_user.id)
    hwids = await manager.get_user_hwids(username)

    target_dev = next((d for d in hwids if d.get("hwid", "").startswith(short_hwid)), None)
    if not target_dev:
        await call.answer("Устройство не найдено или уже было удалено.", show_alert=True)
        await user_devices_page(call)
        return

    full_hwid = target_dev.get("hwid", "")
    model = target_dev.get("device_model") or "Не указана"
    dev_os = target_dev.get("device_os") or "Не указана"
    os_ver = target_dev.get("os_version") or ""
    os_full = f"{dev_os} {os_ver}".strip()

    created_str = target_dev.get("created_at", "")
    if created_str and "T" in str(created_str):
        created_str = str(created_str).split(".")[0].replace("T", " ")
    last_used_str = target_dev.get("last_used_at", "")
    if last_used_str and "T" in str(last_used_str):
        last_used_str = str(last_used_str).split(".")[0].replace("T", " ")
    else:
        last_used_str = "Нет данных"

    lines = [
        "📱 <b>Информация об устройстве</b>\n",
        "<blockquote>"
        f"📱 Модель: <b>{model}</b>\n"
        f"💻 ОС: <b>{os_full}</b>\n"
        f"🔑 HWID: <code>{full_hwid}</code>\n"
        f"📅 Добавлено: <code>{created_str}</code>\n"
        f"⏳ Активность: <code>{last_used_str}</code>"
        "</blockquote>\n",
        "Вы можете удалить это устройство, чтобы освободить место для нового:",
    ]
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.device_details_keyboard(short_hwid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("user:dev_del:") & ~F.data.startswith("user:dev_del_conf:"))
async def user_device_delete_prompt(call: CallbackQuery) -> None:
    short_hwid = call.data.split(":", 2)[2]
    username = db.build_vpn_username(call.from_user.id)
    hwids = await manager.get_user_hwids(username)

    target_dev = next((d for d in hwids if d.get("hwid", "").startswith(short_hwid)), None)
    model = (target_dev.get("device_model") or target_dev.get("device_os") or "это устройство") if target_dev else "это устройство"

    text = (
        "⚠️ <b>Удаление устройства</b>\n\n"
        f"Вы действительно хотите удалить устройство <b>{model}</b>?\n\n"
        "Оно немедленно потеряет доступ к VPN и не сможет подключаться."
    )
    await call.message.edit_text(text, reply_markup=await kbs.device_delete_confirm_keyboard(short_hwid))
    await call.answer()


@router.callback_query(F.data.startswith("user:dev_del_conf:"))
async def user_device_delete_confirm(call: CallbackQuery) -> None:
    short_hwid = call.data.split(":", 2)[2]
    username = db.build_vpn_username(call.from_user.id)
    hwids = await manager.get_user_hwids(username)

    target_dev = next((d for d in hwids if d.get("hwid", "").startswith(short_hwid)), None)
    full_hwid = target_dev.get("hwid") if target_dev else short_hwid

    success = await manager.delete_user_hwid(username, full_hwid)
    if success:
        await call.answer("✅ Устройство успешно удалено!", show_alert=True)
    else:
        await call.answer("Устройство уже было удалено ранее.", show_alert=True)

    await user_devices_page(call)


@router.callback_query(F.data == "user:dev_reset_all")
async def user_devices_reset_all_prompt(call: CallbackQuery) -> None:
    text = (
        "⚠️ <b>Сброс ВСЕХ устройств</b>\n\n"
        "Вы действительно хотите удалить все привязанные устройства?\n\n"
        "Все текущие подключения будут немедленно отключены."
    )
    await call.message.edit_text(text, reply_markup=await kbs.device_reset_all_confirm_keyboard())
    await call.answer()


@router.callback_query(F.data == "user:dev_reset_all_conf")
async def user_devices_reset_all_confirm(call: CallbackQuery) -> None:
    username = db.build_vpn_username(call.from_user.id)
    await manager.reset_user_hwids(username)
    await call.answer("✅ Все устройства успешно сброшены!", show_alert=True)
    await user_devices_page(call)


# ---------------- Subscription Link Revocation ----------------
@router.callback_query(F.data == "user:reset_sub")
async def user_reset_sub_prompt(call: CallbackQuery) -> None:
    text = (
        "🔄 <b>Сброс ссылки на подписку</b>\n\n"
        "<blockquote>⚠️ <b>Внимание:</b> после сброса старая ссылка на подписку и все старые ключи "
        "<b>немедленно перестанут работать</b> на всех ваших устройствах.</blockquote>\n\n"
        "Вам потребуется скопировать новую ссылку и заново импортировать её в приложение VPN.\n\n"
        "Вы уверены, что хотите сбросить ссылку?"
    )
    await call.message.edit_text(text, reply_markup=await kbs.reset_sub_confirm_keyboard())
    await call.answer()


@router.callback_query(F.data == "user:reset_sub_conf")
async def user_reset_sub_confirm(call: CallbackQuery) -> None:
    username = db.build_vpn_username(call.from_user.id)
    try:
        updated = await manager.revoke_user_subscription(username)
        new_url = updated.get("subscription_url") or await manager.get_subscription_link(username)
    except Exception as exc:
        logger.exception("Failed to revoke sub for %s: %s", username, exc)
        await call.answer(f"Ошибка при сбросе ссылки: {exc}", show_alert=True)
        return

    text = (
        "✅ <b>Ссылка на подписку успешно сброшена!</b>\n\n"
        "<blockquote>Старая ссылка деактивирована и больше не работает.</blockquote>\n\n"
        f"🔗 <b>Ваша новая ссылка:</b>\n<code>{new_url}</code>\n\n"
        "Скопируйте новую ссылку и обновите подключение в приложении (Happ, v2rayNG, Streisand, FoXray)."
    )
    builder = InlineKeyboardBuilder()
    if new_url:
        builder.row(InlineKeyboardButton(text="🚀 Открыть новую подписку", url=new_url, style="primary"))
    builder.row(InlineKeyboardButton(text="⚙️ В настройки подписки", callback_data="user:sub_settings"))
    builder.row(InlineKeyboardButton(text="« К подключению", callback_data="user:connect"))
    builder.row(InlineKeyboardButton(text="« Главное меню", callback_data="user:menu"))

    await call.message.edit_text(text, reply_markup=builder.as_markup())
    await call.answer("Ссылка успешно сброшена!")



@router.callback_query(F.data == "user:faq")
async def faq_page(call: CallbackQuery) -> None:
    cfg = await db.get_app_config()
    project_name = cfg.get("project_name", settings.project_name)

    await call.message.edit_text(
        f"<b>ℹ {project_name} — Справка</b>\n\n"
        "<blockquote><b>Бесплатный тариф vs Премиум:</b>\n"
        "• <b>Бесплатный:</b> базовый доступ к серверам aska_free, поддерживается короткой рекламой (3 сек) и каналами-спонсорами.\n"
        "• <b>Премиум:</b> выделенные серверы aska_premium, технология <b>LTE-обходов</b>, максимальная скорость без рекламы и каналов.</blockquote>\n\n"
        "<blockquote><b>Технология LTE-обходов:</b>\n"
        "Специальные порты и маскировка трафика для стабильной работы на мобильных сетях РФ (МТС, МегаФон, Билайн, Tele2, Yota) без ограничений операторов.</blockquote>\n\n"
        "<blockquote><b>Оплата:</b>\n"
        "Оплата картой РФ или СБП через RollyPay, а также мгновенно с баланса кабинета.</blockquote>",
        reply_markup=await kbs.faq_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:instruction")
async def instruction_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>ℹ Инструкция по подключению</b>\n\n"
        "<blockquote>1. Нажмите «Подключиться» в меню.\n"
        "2. Нажмите «Открыть подписку» или скопируйте ссылку.\n"
        "3. Импортируйте профиль в VPN-клиент (Happ, v2rayNG, Streisand, Nekobox).\n"
        "4. Запустите подключение в приложении.</blockquote>",
        reply_markup=await kbs.instruction_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:stats")
async def stats_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>◉ Статистика сервиса</b>\n\n"
        f"<blockquote>Пользователей: <code>{await db.count_users()}</code>\n"
        f"Премиум: <code>{await db.count_premium_users()}</code>\n"
        f"Бесплатных: <code>{await db.count_free_users()}</code>\n"
        f"Каналов-спонсоров: <code>{await db.count_channels()}</code></blockquote>",
        reply_markup=await kbs.stats_keyboard(),
    )
    await call.answer()


# ---------------- Verification & Rules ----------------
@router.callback_query(F.data == "user:check_sub")
async def check_subscriptions(call: CallbackQuery) -> None:
    result = await check_sub(call.from_user.id, call.message)
    if not result.is_subscribed:
        await db.set_user_subscription_state(call.from_user.id, False)
        await call.message.edit_text(
            "<b>◈ Проверка не пройдена</b>\n\nПодпишитесь на каналы спонсоров или выберите Премиум тариф.",
            reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
        )
        await call.answer("Подписка есть еще не на все каналы.", show_alert=True)
        return

    await db.mark_user_registered(call.from_user.id, True)
    tariff = await db.get_user_tariff(call.from_user.id)
    await call.message.edit_text(await _main_menu_text(tariff), reply_markup=await kbs.main_menu(tariff))
    await call.answer("Доступ подтвержден")


@router.callback_query(F.data == "user:accept_rules")
async def accept_rules(call: CallbackQuery) -> None:
    await db.ensure_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    await db.mark_rules_accepted(call.from_user.id)
    result = await check_sub(call.from_user.id, call.message)
    if not result.is_subscribed:
        await db.set_user_subscription_state(call.from_user.id, False)
        await call.message.edit_text(
            "✓ Условия приняты.\n\nТеперь подпишитесь на спонсор-каналы для доступа к бесплатному VPN.",
            reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
        )
        await call.answer("Условия приняты")
        return

    await db.mark_user_registered(call.from_user.id, True)
    tariff = await db.get_user_tariff(call.from_user.id)
    await call.message.edit_text(await _main_menu_text(tariff), reply_markup=await kbs.main_menu(tariff))
    await call.answer("Регистрация завершена")


# ---------------- Support ----------------
@router.callback_query(F.data == "user:support")
async def support_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "<b>✉ Поддержка</b>\n\n"
        "Если у вас возник вопрос по работе сервиса, подключению или оплате, создайте обращение:",
        reply_markup=await kbs.support_page_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:start_support")
async def start_support_request(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "<b>Шаг 1 из 2: Выберите тему обращения</b>\n\n"
        "Выберите готовую тему из списка ниже или нажмите «✏️ Другая тема», чтобы ввести её вручную:",
        reply_markup=await kbs.support_topics_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("support:topic:"))
async def support_topic_selected(call: CallbackQuery, state: FSMContext) -> None:
    topic_key = call.data.split(":", 2)[2]
    if topic_key == "custom":
        await state.set_state(SupportRequest.title)
        await call.message.edit_text(
            "<b>Шаг 1 из 2: Тема обращения</b>\n\n"
            "Введите тему вашего обращения сообщением в чат:\n"
            "<i>(например: Ошибка при подключении на iPhone)</i>",
            reply_markup=await kbs.support_cancel_keyboard(),
        )
        await call.answer()
        return

    topic_title = kbs.SUPPORT_TOPIC_MAP.get(topic_key, "❓ Общий вопрос")
    await state.update_data(title=topic_title)
    await state.set_state(SupportRequest.question)
    await call.message.edit_text(
        f"<b>Выбранная тема:</b> {topic_title}\n\n"
        "<b>Шаг 2 из 2: Подробности обращения</b>\n\n"
        "Опишите вашу проблему или вопрос сообщением в чат:\n\n"
        "<i>Чем подробнее описание, тем быстрее поддержка сможет вам помочь.</i>",
        reply_markup=await kbs.support_cancel_keyboard(),
    )
    await call.answer()


@router.message(SupportRequest.title)
async def support_title_step(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Пожалуйста, отправьте тему обращения текстом:",
            reply_markup=await kbs.support_cancel_keyboard(),
        )
        return
    await state.update_data(title=text)
    await state.set_state(SupportRequest.question)
    await message.answer(
        f"<b>Тема:</b> <code>{text}</code>\n\n"
        "<b>Шаг 2 из 2: Подробности обращения</b>\n\n"
        "Опишите вашу проблему или вопрос сообщением в чат:\n\n"
        "<i>Чем подробнее описание, тем быстрее поддержка сможет вам помочь.</i>",
        reply_markup=await kbs.support_cancel_keyboard(),
    )


@router.message(SupportRequest.question)
async def support_text_step(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Пожалуйста, опишите ваше обращение текстом:",
            reply_markup=await kbs.support_cancel_keyboard(),
        )
        return
    await state.update_data(text=text)
    data = await state.get_data()
    title = data.get("title", "Без темы")
    await state.set_state(SupportRequest.confirm)
    await message.answer(
        "<b>Проверьте обращение перед отправкой</b>\n\n"
        f"<b>Тема:</b> <code>{title}</code>\n"
        f"<b>Текст:</b> <i>{text}</i>\n\n"
        "Отправить это обращение в службу поддержки?",
        reply_markup=await kbs.support_confirm_keyboard(),
    )


@router.callback_query(SupportRequest.confirm, F.data == "support:send")
async def support_send(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    title = data.get("title", "Без темы")
    text = data.get("text", "")
    recipients = await db.list_support_recipient_ids()
    if not recipients:
        await call.message.edit_text(
            "Сейчас нет доступных сотрудников поддержки. Попробуйте позже.",
            reply_markup=await kbs.support_page_keyboard(),
        )
        await state.clear()
        return

    ticket = await db.create_support_ticket(
        user_id=call.from_user.id,
        username=call.from_user.username,
        title=title,
        text=text,
    )
    delivered = 0
    for recipient_id in recipients:
        try:
            await call.bot.send_message(
                recipient_id,
                (
                    f"<b>Вопрос #{ticket['ticket_id']}</b>\n"
                    f"<b>От:</b> <code>{call.from_user.id}</code>\n"
                    f"<b>Тема:</b> {title}\n\n"
                    f"<i>{text}</i>"
                ),
                reply_markup=await kbs.support_request_keyboard(call.from_user.id, ticket["ticket_id"]),
            )
            delivered += 1
        except Exception:
            continue

    if delivered == 0:
        await call.message.edit_text(
            "Сейчас никто из администраторов не может получить обращение. Попробуйте позже.",
            reply_markup=await kbs.support_page_keyboard(),
        )
        await state.clear()
        return

    await state.clear()
    await call.message.edit_text(
        f"✓ Вопрос #{ticket['ticket_id']} отправлен. Ответ поступит в этот чат.",
        reply_markup=await kbs.support_page_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "support:cancel")
async def support_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Создание обращения отменено.", reply_markup=await kbs.support_page_keyboard())
    await call.answer()


@router.callback_query(F.data == "copy_link")
async def legacy_copy_link(call: CallbackQuery) -> None:
    await connect_page(call)
