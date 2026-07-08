from __future__ import annotations

import asyncio
import random
from html import escape
from datetime import datetime, timedelta, timezone
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from config import settings
from core.app_core import MarzbanError, manager
from database.db import db
from utils import kbs
from utils.checksub import SubscriptionCheckResult, check_sub

router = Router()


class SupportRequest(StatesGroup):
    # Три шага: тема, текст и подтверждение отправки.
    title = State()
    question = State()
    confirm = State()


def _main_menu_text() -> str:
    if settings.menu_image_url:
        return f'<b><a href="{settings.menu_image_url}">⚡️</a> Главное меню</b>'
    return f"<b>{settings.project_name}</b>"


def _registration_text() -> str:
    lines = [
        "<b>Регистрация</b>",
        "",
        "Перед использованием бота нужно принять условия сервиса.",
        "Откройте документы по кнопкам ниже и подтвердите согласие.",
    ]
    return "\n".join(lines)


def _format_expire(timestamp: int | None) -> str:
    timestamp = manager._normalize_expire(timestamp)
    if not timestamp:
        return "Без срока"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


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
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _renewal_state(user_info: dict, local_user: dict | None) -> dict[str, object]:
    # Здесь собрана вся анти-фарм логика бесплатного продления.
    expire_ts = manager._normalize_expire(user_info.get("expire")) or 0
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
            "💠 <b>Бесплатное продление откроется за</b> "
            f"{settings.free_renew_window_hours} ч. до конца подписки: "
            f"{_format_dt(expire_at - renew_window)}"
        )
    elif not cooldown_ready and next_free_renew_at:
        reason = (
            "💠<b>Бесплатное продление доступно не чаще одного раза в</b> "
            f"{settings.free_renew_cooldown_days} дней. Следующая попытка: {_format_dt(next_free_renew_at)}"
        )
    else:
        reason = "💠 <b>Можно продлить подписку после</b> повторной проверки споносор-каналов."

    return {
        "show_button": show_button,
        "can_renew": within_window and cooldown_ready,
        "reason": reason,
        "last_free_renew_at": last_free_renew_at,
    }


async def _animate_loading(message: Message, label: str) -> Message:
    # Небольшая анимация возвращает прежний UX и делает ожидание не таким сухим.
    status_message = await message.edit_text(f"⏳ {label}...")
    steps = random.choices(["⚡️", "🧩", "🌐", "📶", "🔮", "➕"], k=random.randint(3, 5))
    for index, emoji in enumerate(steps, start=1):
        await status_message.edit_text(f"{emoji} <b>{label}</b>{'.' * index}")
        await asyncio.sleep(0.18)
    return status_message


async def _show_sponsors(message: Message, result: SubscriptionCheckResult | None = None) -> None:
    channels = await db.list_channels()
    if not channels:
        await message.answer("Сейчас спонсор-каналы не настроены.", reply_markup=await kbs.main_menu())
        return

    text = (
        "🔑 <b>Доступ закрыт</b>\n\n"
        "Подпишитесь на спонсор-каналы для доступа к бесплатному VPN."
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

    await db.ensure_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
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

    result = await check_sub(call.from_user.id, call.message)
    if result.is_subscribed:
        await db.mark_user_registered(call.from_user.id, True)
        return True

    await db.set_user_subscription_state(call.from_user.id, False)
    await call.message.edit_text(
        "🔑 <b>Доступ закрыт</b>\n\nПодпишитесь на спонсор-каналы для доступа.",
        reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
    )
    await call.answer()
    return False


async def _load_subscription_context(user_id: int) -> tuple[dict, dict, dict[str, object]]:
    local_user = await db.get_user_by_id(user_id)
    user_info = await manager.ensure_subscription_user(
        username=db.build_vpn_username(user_id),
        data_limit_gb=settings.trial_limit_gb,
        expire_days=settings.trial_days,
        note=f"Telegram user {user_id}",
    )
    return local_user or {}, user_info, _renewal_state(user_info, local_user)


def _subscription_text(user_info: dict, user_id: int, renewal: dict[str, object]) -> str:
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
    lines = [
        "<b>📊 Подписка</b>",
        "",
        f"<blockquote>🧩 Профиль: <code>{db.build_vpn_username(user_id)}</code>",
        f"🗽 <b>Статус:</b> <b>{status_map.get(user_info.get('status'), user_info.get('status', 'unknown'))}</b>",
        f"🚦 <b>Трафик:</b> <code>{used} / {limit}</code>",
        f"<b>🗓 Действует до:</b> <code>{_format_expire(user_info.get('expire'))}</code></blockquote>",
        "",
        str(renewal["reason"]),
    ]
    if renewal.get("last_free_renew_at"):
        lines.append(f"🗓 <b>Последнее бесплатное продление:</b> <code>{_format_dt(renewal['last_free_renew_at'])}</code>")
    return "\n".join(lines)


def _format_device_dt(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _devices_text(user_info: dict, devices: list[dict]) -> str:
    limit = user_info.get("hwid_limit")
    limit_text = str(limit) if limit is not None else "без лимита"
    lines = [
        "<b>📱 Устройства</b>",
        "",
        f"Лимит: <code>{limit_text}</code>",
        f"Подключено: <code>{len(devices)}</code>",
    ]
    if not devices:
        lines.append("\nУстройства пока не привязаны к подписке.")
        return "\n".join(lines)

    for index, device in enumerate(devices, start=1):
        title = device.get("device_model") or device.get("device_os") or "Неизвестное устройство"
        lines.extend(
            [
                "",
                f"<b>{index}. {escape(str(title))}</b>",
                f"OS: <code>{escape(str(device.get('device_os') or 'n/a'))}</code>",
                f"HWID: <code>{escape(str(device.get('hwid') or 'n/a'))}</code>",
                f"Последнее использование: <code>{escape(_format_device_dt(device.get('last_used_at')))}</code>",
            ]
        )
    return "\n".join(lines)


def _sub_updates_text(updates: list[dict]) -> str:
    lines = ["<b>🕓 Обновления подписки</b>"]
    if not updates:
        lines.append("\nИстория обновлений пока пустая.")
        return "\n".join(lines)

    for index, item in enumerate(updates, start=1):
        user_agent = str(item.get("user_agent") or "unknown")
        ip = item.get("ip") or "n/a"
        hwid = item.get("hwid") or "n/a"
        lines.extend(
            [
                "",
                f"<b>{index}. {_format_device_dt(item.get('created_at'))}</b>",
                f"IP: <code>{escape(str(ip))}</code>",
                f"HWID: <code>{escape(str(hwid))}</code>",
                f"Client: <code>{escape(user_agent[:120])}</code>",
            ]
        )
    return "\n".join(lines)


async def _send_support_ticket(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    title = data["title"]
    text = data["text"]
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
        f"✅ Вопрос #{ticket['ticket_id']} отправлен. Ответ придет в этот чат.",
        reply_markup=await kbs.support_page_keyboard(),
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def start_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _ensure_access(message):
        return

    await message.answer(_main_menu_text(), reply_markup=await kbs.main_menu())


@router.callback_query(F.data == "user:menu")
async def menu_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _require_sponsors(call):
        return
    await call.message.edit_text(_main_menu_text(), reply_markup=await kbs.main_menu())
    await call.answer()


@router.callback_query(F.data == "user:check_sub")
async def check_subscriptions(call: CallbackQuery) -> None:
    result = await check_sub(call.from_user.id, call.message)
    if not result.is_subscribed:
        await db.set_user_subscription_state(call.from_user.id, False)
        await call.message.edit_text(
            "🔑 <b>Проверка не пройдена</b>\n\nПодпишитесь на все sponsor-каналы и повторите попытку.",
            reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
        )
        await call.answer("Подписка есть еще не на все каналы.", show_alert=True)
        return

    await db.mark_user_registered(call.from_user.id, True)
    await call.message.edit_text(_main_menu_text(), reply_markup=await kbs.main_menu())
    await call.answer("Спасибо! Доступ подтвержден.")


@router.callback_query(F.data == "user:accept_rules")
async def accept_rules(call: CallbackQuery) -> None:
    # Принятие правил хранится отдельно от sponsor-check, чтобы онбординг был двухшаговым и прозрачным.
    await db.ensure_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    await db.mark_rules_accepted(call.from_user.id)
    result = await check_sub(call.from_user.id, call.message)
    if not result.is_subscribed:
        await db.set_user_subscription_state(call.from_user.id, False)
        await call.message.edit_text(
            "✅ Условия приняты.\n\nТеперь подпишитесь на sponsor-каналы для доступа.",
            reply_markup=await kbs.sponsors_keyboard(await db.list_channels()),
        )
        await call.answer("Условия приняты")
        return

    await db.mark_user_registered(call.from_user.id, True)
    await call.message.edit_text(_main_menu_text(), reply_markup=await kbs.main_menu())
    await call.answer("Регистрация завершена")


@router.callback_query(F.data.in_({"user:sub", "my_sub"}))
async def subscription_page(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    alert = await db.get_active_subscription_alert()
    if alert:
        await call.answer(alert["text"], show_alert=True)

    await _animate_loading(call.message, "Подписка")
    try:
        _, user_info, renewal = await _load_subscription_context(call.from_user.id)
    except MarzbanError as exc:
        await call.message.edit_text(f"Не удалось получить подписку: {exc.detail}", reply_markup=await kbs.profile_keyboard())
        if not alert:
            await call.answer()
        return

    await call.message.edit_text(
        _subscription_text(user_info, call.from_user.id, renewal),
        reply_markup=await kbs.subscription_keyboard(
            user_info.get("subscription_url", ""),
            show_free_renew=bool(renewal["show_button"]),
        ),
    )
    if not alert:
        await call.answer()


@router.callback_query(F.data == "user:renew_free")
async def renew_free_subscription(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    try:
        _, user_info, renewal = await _load_subscription_context(call.from_user.id)
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
    local_user = await db.get_user_by_id(call.from_user.id)
    renewal = _renewal_state(user_info, local_user)
    await call.message.edit_text(
        _subscription_text(user_info, call.from_user.id, renewal),
        reply_markup=await kbs.subscription_keyboard(
            user_info.get("subscription_url", ""),
            show_free_renew=bool(renewal["show_button"]),
        ),
    )
    await call.answer("Подписка продлена")


@router.callback_query(F.data == "user:devices")
async def devices_page(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    username = db.build_vpn_username(call.from_user.id)
    try:
        user_info = await manager.ensure_subscription_user(
            username=username,
            data_limit_gb=settings.trial_limit_gb,
            expire_days=settings.trial_days,
            note=f"Telegram user {call.from_user.id}",
        )
        devices = await manager.get_user_hwids(user_info["id"])
    except MarzbanError as exc:
        await call.message.edit_text(f"Не удалось получить устройства: {exc.detail}", reply_markup=await kbs.subscription_keyboard(""))
        await call.answer()
        return

    await call.message.edit_text(_devices_text(user_info, devices), reply_markup=await kbs.devices_keyboard(devices))
    await call.answer()


@router.callback_query(F.data.startswith("user:device_del:"))
async def delete_device(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    _, _, raw_index = call.data.split(":")
    username = db.build_vpn_username(call.from_user.id)
    try:
        user_info = await manager.get_user(username)
        devices = await manager.get_user_hwids(user_info["id"])
        index = int(raw_index)
        if index < 0 or index >= len(devices):
            await call.answer("Устройство уже не найдено", show_alert=True)
            return
        await manager.delete_user_hwid(user_info["id"], devices[index]["hwid"])
        devices = await manager.get_user_hwids(user_info["id"])
    except (ValueError, KeyError):
        await call.answer("Не удалось определить устройство", show_alert=True)
        return
    except MarzbanError as exc:
        await call.answer(f"Ошибка: {exc.detail}", show_alert=True)
        return

    await call.message.edit_text(_devices_text(user_info, devices), reply_markup=await kbs.devices_keyboard(devices))
    await call.answer("Устройство удалено")


@router.callback_query(F.data == "user:devices_reset")
async def reset_devices(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    username = db.build_vpn_username(call.from_user.id)
    try:
        user_info = await manager.get_user(username)
        await manager.reset_user_hwids(user_info["id"])
        devices = await manager.get_user_hwids(user_info["id"])
    except MarzbanError as exc:
        await call.answer(f"Ошибка: {exc.detail}", show_alert=True)
        return

    await call.message.edit_text(_devices_text(user_info, devices), reply_markup=await kbs.devices_keyboard(devices))
    await call.answer("Устройства сброшены")


@router.callback_query(F.data == "user:sub_updates")
async def subscription_updates_page(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    username = db.build_vpn_username(call.from_user.id)
    try:
        await manager.ensure_subscription_user(
            username=username,
            data_limit_gb=settings.trial_limit_gb,
            expire_days=settings.trial_days,
            note=f"Telegram user {call.from_user.id}",
        )
        updates = await manager.get_user_subscription_updates(username)
    except MarzbanError as exc:
        await call.message.edit_text(f"Не удалось получить историю: {exc.detail}", reply_markup=await kbs.profile_keyboard())
        await call.answer()
        return

    await call.message.edit_text(_sub_updates_text(updates), reply_markup=await kbs.profile_keyboard())
    await call.answer()


@router.callback_query(F.data == "user:revoke_sub")
async def revoke_subscription_confirm(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    await call.message.edit_text(
        "<b>🔐 Сбросить ссылку подписки?</b>\n\n"
        "Старая ссылка перестанет работать. После сброса откройте подписку заново и импортируйте новый URL в приложение.",
        reply_markup=await kbs.revoke_subscription_confirm_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:revoke_sub_confirm")
async def revoke_subscription(call: CallbackQuery) -> None:
    if not await _require_sponsors(call):
        return

    username = db.build_vpn_username(call.from_user.id)
    try:
        await manager.revoke_subscription(username)
        _, user_info, renewal = await _load_subscription_context(call.from_user.id)
    except MarzbanError as exc:
        await call.answer(f"Ошибка: {exc.detail}", show_alert=True)
        return

    await call.message.edit_text(
        _subscription_text(user_info, call.from_user.id, renewal),
        reply_markup=await kbs.subscription_keyboard(
            user_info.get("subscription_url", ""),
            show_free_renew=bool(renewal["show_button"]),
        ),
    )
    await call.answer("Ссылка обновлена")


@router.callback_query(F.data == "user:faq")
async def faq_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        f"<b>🧩 {settings.project_name} - FAQ</b>\n\n"
        "<blockquote>Бесплатный VPN работает за счет sponsor-модели и рекламы.\n"
        "Бесплатное продление открывается <b>только перед окончанием подписки и только после повторной проверки sponsor-каналов.</b>\n"
        f"Окно продления: {settings.free_renew_window_hours} ч. до конца подписки.\n"
        f"Ограничение: не чаще одного раза в {settings.free_renew_cooldown_days} дней.</blockquote>",
        reply_markup=await kbs.faq_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:profile")
async def profile_page(call: CallbackQuery) -> None:
    local_user = await db.get_user_by_id(call.from_user.id)
    if not local_user:
        await db.ensure_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
        local_user = await db.get_user_by_id(call.from_user.id)

    lines = [
        "<b>👤 Профиль</b>",
        "",
        f"Telegram ID: <code>{local_user['user_id']}</code>",
    ]
    if local_user.get("username"):
        lines.append(f"Username: @{local_user['username']}")
    lines.extend(
        [
            f"<blockquote><b>🔑 Имя:</b> {local_user.get('full_name') or 'n/a'}",
            f"✅ <b>Статус:</b> {local_user.get('status', 'active')}",
            f"⚒️ <b>Условия приняты:</b> {_format_dt(_normalize_dt(local_user.get('accepted_rules_at')))}",
            f"🕸 <b>Sponsor-check:</b> {'пройден' if local_user.get('is_subscribed_to_sponsors') else 'не пройден'}",
            f"🧩 <b>Последнее бесплатное продление:</b> {_format_dt(_normalize_dt(local_user.get('last_free_renew_at')))}</blockquote>",
        ]
    )
    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.profile_keyboard())
    await call.answer()


@router.callback_query(F.data == "user:support")
async def support_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "<b>💡 Поддержка</b>\n\n"
        "Если нужен платный тариф, помощь с подключением или есть вопрос по проекту, можно создать обращение прямо в боте.",
        reply_markup=await kbs.support_page_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:start_support")
async def start_support_request(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportRequest.title)
    await call.message.edit_text("Шаг 1 из 3\n\nВведите тему обращения:", reply_markup=await kbs.profile_keyboard())
    await call.answer()


@router.message(SupportRequest.title)
async def support_title_step(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(SupportRequest.question)
    await message.answer("Шаг 2 из 3\n\nВведите текст обращения:", reply_markup=await kbs.profile_keyboard())


@router.message(SupportRequest.question)
async def support_text_step(message: Message, state: FSMContext) -> None:
    await state.update_data(text=message.text.strip())
    data = await state.get_data()
    await state.set_state(SupportRequest.confirm)
    await message.answer(
        "<b>Проверьте обращение</b>\n\n"
        f"<b>Тема:</b> <code>{data['title']}</code>\n"
        f"<b>Текст:</b> <i>{data['text']}</i>\n\n"
        "Отправить это поддержке?",
        reply_markup=await kbs.support_confirm_keyboard(),
    )


@router.callback_query(SupportRequest.confirm, F.data == "support:send")
async def support_send(call: CallbackQuery, state: FSMContext) -> None:
    await _send_support_ticket(call, state)
    await call.answer()


@router.callback_query(F.data == "support:cancel")
async def support_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Создание обращения отменено.", reply_markup=await kbs.support_page_keyboard())
    await call.answer()


@router.callback_query(F.data == "user:stats")
async def stats_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>⚡️ Статистика</b>\n\n"
        f"<blockquote>🫂 <b>Активных пользователей:</b> <code>{await db.count_users()}</code>\n"
        f"👨‍💻 <b>Администраторов:</b> <code>{await db.count_admins()}</code>\n"
        f"<b>💠 Рекламных кнопок:</b> <code>{await db.count_ads()}</code>\n"
        f"<b>💠 Sponsor-каналов:</b> <code>{await db.count_channels()}</code>\n\n</blockquote>"
        "<b><i>Вместе с аудиторией проект становится сильнее.</i></b>",
        reply_markup=await kbs.stats_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "user:donate")
async def donate_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>❓ Почему я вижу рекламу</b>\n\n"
        "Рекламные размещения помогают оплачивать серверы и держать бесплатный тариф живым.\n"
        "Если хотите поддержать проект напрямую, используйте кнопки ниже.",
        reply_markup=await kbs.donate_keyboard("user:menu"),
    )
    await call.answer()


@router.callback_query(F.data == "user:donate_help")
async def donate_help_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>❤️ Как помочь проекту</b>\n\n"
        "Можно поддержать проект донатом или обратиться в поддержку, если нужен другой формат сотрудничества.",
        reply_markup=await kbs.donate_keyboard("user:faq"),
    )
    await call.answer()


@router.callback_query(F.data == "user:instruction")
async def instruction_page(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>🛠 Инструкция по подключению</b>\n\n"
        "<blockquote><i>1. Откройте страницу подписки в разделе 'Подписка'.\n"
        "2. Импортируйте конфигурацию в VPN-приложение.\n"
        "3. Подтвердите создание VPN-подключения.\n\n</i></blockquote>"
        "<b>Если нужна внешняя подробная инструкция, используйте кнопку 'Инструкция' на странице подписки.</b>",
        reply_markup=await kbs.instruction_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "copy_link")
async def legacy_copy_link(call: CallbackQuery) -> None:
    await subscription_page(call)
