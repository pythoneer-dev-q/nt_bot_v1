from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from html import escape
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import settings
from core.app_core import MarzbanError, UserStatus, manager, safe_int_ts
from core.tariffs import calculate_tariffs
from database.db import db
from database.migration import export_database
from filters import AdminFilter
from services import broadcast_service
from services.backups import backup_service
from services.subscriptions import subscription_maintenance_service
from utils import kbs

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class UserLookupState(StatesGroup):
    waiting_for_query = State()


class AddAdminState(StatesGroup):
    waiting_for_user_id = State()


class AddChannelState(StatesGroup):
    waiting_for_channel = State()


class AddAdState(StatesGroup):
    waiting_for_ad = State()


class BlockUserState(StatesGroup):
    waiting_for_reason = State()


class SubscriptionAlertState(StatesGroup):
    waiting_for_alert = State()


class BroadcastState(StatesGroup):
    waiting_for_text = State()
    waiting_for_button = State()
    ready = State()


class SupportReplyState(StatesGroup):
    waiting_for_text = State()


class SupportDeclineState(StatesGroup):
    waiting_for_text = State()


class SetBasePriceState(StatesGroup):
    waiting_for_price = State()


class SetDiscountPercentState(StatesGroup):
    waiting_for_percent = State()


class SetFreeDeviceLimitState(StatesGroup):
    waiting_for_limit = State()


class SetPremiumDeviceLimitState(StatesGroup):
    waiting_for_limit = State()


class SetUserDeviceLimitState(StatesGroup):
    waiting_for_limit = State()


def _format_dt(value: datetime | None) -> str:
    if not value:
        return "n/a"
    return value.strftime("%d.%m.%Y %H:%M")


def _broadcast_buttons_text(buttons: list[dict[str, str]]) -> str:
    if not buttons:
        return "Кнопки: не добавлены"
    return "Кнопки:\n" + "\n".join(f"{index}. {button['text']} -> {button['value']}" for index, button in enumerate(buttons, start=1))


def _json_block(title: str, payload: object) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return f"<b>{escape(title)}</b>\n{escape(rendered)}"


def _split_health_text(sections: list[str], limit: int = 3600) -> list[str]:
    chunks: list[str] = []
    current = ""

    for section in sections:
        parts = section.splitlines(keepends=True) or [section]
        for part in parts:
            while len(part) > limit:
                if current:
                    chunks.append(current.rstrip())
                    current = ""
                chunks.append(part[:limit].rstrip())
                part = part[limit:]

            if len(current) + len(part) > limit:
                chunks.append(current.rstrip())
                current = ""
            current += part
        current += "\n"

    if current.strip():
        chunks.append(current.rstrip())
    return chunks


async def _user_card(user_id: int) -> str:
    user = await db.get_user_by_id(user_id)
    username = db.build_vpn_username(user_id)
    vpn_status = "Профиль еще не создан"
    traffic = "n/a"
    expire = "n/a"
    group_str = "n/a"

    try:
        vpn_user = await manager.get_user_or_none(username)
        if vpn_user:
            traffic_limit = vpn_user.get("data_limit", 0)
            vpn_status = vpn_user.get("status", "unknown")
            traffic = (
                f"{manager.format_bytes(vpn_user.get('used_traffic', 0))} / "
                f"{manager.format_bytes(traffic_limit) if traffic_limit else 'Безлимит'}"
            )
            expire_ts = safe_int_ts(vpn_user.get("expire"))
            expire = datetime.fromtimestamp(expire_ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M") if expire_ts else "Без срока"
            group_ids = vpn_user.get("group_ids") or []
            group_str = f"IDs {group_ids}" if group_ids else "Нет группы"
    except MarzbanError as exc:
        vpn_status = f"Ошибка PasarGuard: {exc.detail}"

    tariff = await db.get_user_tariff(user_id) if user else "free"
    tariff_title = "✦ Премиум" if tariff == "premium" else "Бесплатный"

    if not user:
        return (
            "<b>◈ Пользователь не найден в боте</b>\n\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"Тариф: <b>{tariff_title}</b>\n"
            f"VPN username: <code>{username}</code>\n"
            f"VPN статус: {vpn_status}\n"
            f"Группа: {group_str}\n"
            f"Трафик: {traffic}\n"
            f"Подписка до: {expire}"
        )

    lines = [
        "<b>◈ Карточка пользователя</b>",
        "",
        f"Telegram ID: <code>{user['user_id']}</code>",
    ]
    if user.get("username"):
        lines.append(f"Username: @{user['username']}")
    lines.extend(
        [
            f"Имя: {user.get('full_name') or 'n/a'}",
            f"Тариф: <b>{tariff_title}</b>",
            f"Статус в боте: {user.get('status', 'active')}",
            f"Зарегистрирован: {_format_dt(user.get('registered_at') or user.get('created_at'))}",
            f"Sponsor-check: {'да' if user.get('is_subscribed_to_sponsors') else 'нет'}",
            f"VPN username: <code>{username}</code>",
            f"VPN статус: {vpn_status}",
            f"Группа в PasarGuard: {group_str}",
            f"Трафик: {traffic}",
            f"Подписка до: {expire}",
        ]
    )
    device_limit = await db.get_user_device_limit(user_id) if user else settings.free_device_limit
    custom_limit = user.get("custom_device_limit") if user else None
    limit_note = f"{device_limit} (индивидуальный)" if custom_limit is not None else f"{device_limit} (по тарифу)"
    hwids = await manager.get_user_hwids(username)
    lines.append(f"Лимит устройств: <b>{limit_note}</b>")
    lines.append(f"Подключено устройств: <b>{len(hwids)}</b>")
    if user.get("premium_expires_at"):
        lines.append(f"Премиум до: {_format_dt(user['premium_expires_at'])}")
    if user.get("blocked_reason"):
        lines.append(f"Причина блокировки: {user['blocked_reason']}")
    return "\n".join(lines)


async def _show_channels(call: CallbackQuery) -> None:
    channels = await db.list_channels()
    if not channels:
        await call.message.edit_text("» Sponsor-каналы пока не добавлены.", reply_markup=await kbs.sponsor_channels_list(channels))
        return

    lines = ["<b>» Sponsor-каналы</b>"]
    for channel in channels:
        lines.append(f"{channel.get('title', channel['channel_ref'])} -> <code>{channel['channel_ref']}</code>")
    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.sponsor_channels_list(channels))


async def _show_ads(call: CallbackQuery) -> None:
    ads = await db.list_ads()
    if not ads:
        await call.message.edit_text("✦ Рекламные кнопки еще не добавлены.", reply_markup=await kbs.ads_list_keyboard(ads))
        return

    lines = ["<b>✦ Рекламные кнопки (показываются бесплатным пользователям)</b>"]
    for ad in ads:
        lines.append(f"{ad['text']} -> {ad['url']}")
    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.ads_list_keyboard(ads))


async def _send_broadcast_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text")
    buttons = data.get("broadcast_buttons", [])
    if not text:
        await message.answer("Сначала введите текст рассылки.")
        return

    await message.answer("<b>✉ Превью рассылки</b>")
    await message.answer(text, reply_markup=kbs.build_inline_markup(buttons))
    await message.answer(_broadcast_buttons_text(buttons), reply_markup=await kbs.broadcast_menu(buttons))


# ---------------- Admin Entry ----------------
@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("◈ Админ-панель", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data == "admin:panel")
async def admin_panel_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("◈ Админ-панель", reply_markup=await kbs.admin_panel())
    await call.answer()


# ---------------- Pricing & Discount Management ----------------
@router.callback_query(F.data == "admin:pricing")
async def admin_pricing_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    cfg = await db.get_app_config()
    base_price = cfg["premium_base_price_rub"]
    discount_base = cfg["default_discount_percent"]
    options = calculate_tariffs(base_price, discount_base)

    lines = [
        "<b>[₽] Настройки тарифов и скидок</b>",
        "",
        f"Базовая цена за 1 месяц: <b>{base_price:.2f} ₽</b> (дефолт в .env)",
        f"Базовый процент скидки: <b>{discount_base:.1f}%</b> (дефолт в .env)",
        "<i>Скидка с каждым шагом удваивается: 0% → D% → 2D% → 4D%</i>",
        "",
        "<b>◉ Расчетная сетка тарифов:</b>",
    ]

    for opt in options:
        if opt.discount_percent > 0:
            lines.append(
                f"• <b>{opt.title}:</b> {opt.total_price} ₽ "
                f"(-{int(opt.discount_percent)}%, {opt.price_per_month} ₽/мес, экономия {opt.savings} ₽)"
            )
        else:
            lines.append(f"• <b>{opt.title}:</b> {opt.total_price} ₽ (базовая ставка, {opt.price_per_month} ₽/мес)")

    lines.extend(
        [
            "",
            "Изменения сохраняются в БД и сразу применяются для всех пользователей.",
        ]
    )

    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.admin_pricing_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:set_base_price")
async def admin_set_base_price_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetBasePriceState.waiting_for_price)
    await call.message.edit_text(
        "Введите новую базовую цену за 1 месяц Премиума в рублях (например: <code>199</code> или <code>249</code>):",
        reply_markup=await kbs.admin_pricing_keyboard(),
    )
    await call.answer()


@router.message(SetBasePriceState.waiting_for_price)
async def admin_set_base_price_process(message: Message, state: FSMContext) -> None:
    raw_text = message.text.strip().replace(",", ".")
    try:
        val = float(raw_text)
        if val <= 0 or val > 100000:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите корректное положительное число (рубли).")
        return

    await db.update_app_config(premium_base_price_rub=round(val, 2))
    await state.clear()
    await message.answer(f"✓ Базовая цена успешно обновлена: <b>{val:.2f} ₽</b>")

    # Show updated pricing table
    cfg = await db.get_app_config()
    options = calculate_tariffs(cfg["premium_base_price_rub"], cfg["default_discount_percent"])
    lines = ["<b>◉ Обновленная сетка тарифов:</b>"]
    for opt in options:
        lines.append(f"• {opt.title}: <b>{opt.total_price} ₽</b> (-{int(opt.discount_percent)}%, {opt.price_per_month} ₽/мес)")
    await message.answer("\n".join(lines), reply_markup=await kbs.admin_pricing_keyboard())


@router.callback_query(F.data == "admin:set_discount_percent")
async def admin_set_discount_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetDiscountPercentState.waiting_for_percent)
    await call.message.edit_text(
        "Введите базовый процент скидки (для 3 месяцев, например <code>10</code> или <code>15</code>):\n\n"
        "<i>На 6 месяцев скидка удвоится (2x), а на 12 месяцев — учетверится (4x).</i>",
        reply_markup=await kbs.admin_pricing_keyboard(),
    )
    await call.answer()


@router.message(SetDiscountPercentState.waiting_for_percent)
async def admin_set_discount_process(message: Message, state: FSMContext) -> None:
    raw_text = message.text.strip().replace(",", ".").replace("%", "")
    try:
        val = float(raw_text)
        if val < 0 or val > 50:
            raise ValueError
    except ValueError:
        await message.answer("Процент скидки должен быть числом от 0 до 50.")
        return

    await db.update_app_config(default_discount_percent=round(val, 1))
    await state.clear()
    await message.answer(f"✓ Базовый процент скидки успешно обновлен: <b>{val:.1f}%</b>")

    # Show updated pricing table
    cfg = await db.get_app_config()
    options = calculate_tariffs(cfg["premium_base_price_rub"], cfg["default_discount_percent"])
    lines = ["<b>◉ Обновленная сетка тарифов:</b>"]
    for opt in options:
        lines.append(f"• {opt.title}: <b>{opt.total_price} ₽</b> (-{int(opt.discount_percent)}%, {opt.price_per_month} ₽/мес)")
    await message.answer("\n".join(lines), reply_markup=await kbs.admin_pricing_keyboard())


# ---------------- Global Device Limits Management ----------------
@router.callback_query(F.data == "admin:device_limits")
async def admin_device_limits_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    cfg = await db.get_app_config()
    free_limit = cfg.get("free_device_limit", settings.free_device_limit)
    premium_limit = cfg.get("premium_device_limit", settings.premium_device_limit)

    lines = [
        "📱 <b>Настройки лимитов устройств</b>",
        "",
        f"Лимит для Бесплатного тарифа (Free): <b>{free_limit}</b>",
        f"Лимит для Премиум тарифа (Premium): <b>{premium_limit}</b>",
        "",
        "<i>Лимит определяет, сколько одновременных устройств (HWID) может подключить пользователь. "
        "При достижении лимита бот предупреждает пользователя и запрещает новые подключения.</i>",
        "",
        "Выберите, какой лимит вы хотите изменить:",
    ]
    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.admin_device_limits_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:set_free_device_limit")
async def admin_set_free_device_limit_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetFreeDeviceLimitState.waiting_for_limit)
    await call.message.edit_text(
        "Введите новый лимит устройств для <b>Бесплатного тарифа</b> (целое число от 1 до 50):",
        reply_markup=await kbs.admin_device_limits_keyboard(),
    )
    await call.answer()


@router.message(SetFreeDeviceLimitState.waiting_for_limit)
async def admin_set_free_device_limit_process(message: Message, state: FSMContext) -> None:
    try:
        val = int(message.text.strip())
        if val < 1 or val > 50:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите целое число от 1 до 50.")
        return

    await db.update_app_config(free_device_limit=val)
    await state.clear()
    await message.answer(f"✅ Лимит устройств для Free-тарифа обновлен: <b>{val}</b>", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data == "admin:set_premium_device_limit")
async def admin_set_premium_device_limit_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetPremiumDeviceLimitState.waiting_for_limit)
    await call.message.edit_text(
        "Введите новый лимит устройств для <b>Премиум тарифа</b> (целое число от 1 до 50):",
        reply_markup=await kbs.admin_device_limits_keyboard(),
    )
    await call.answer()


@router.message(SetPremiumDeviceLimitState.waiting_for_limit)
async def admin_set_premium_device_limit_process(message: Message, state: FSMContext) -> None:
    try:
        val = int(message.text.strip())
        if val < 1 or val > 50:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите целое число от 1 до 50.")
        return

    await db.update_app_config(premium_device_limit=val)
    await state.clear()
    await message.answer(f"✅ Лимит устройств для Premium-тарифа обновлен: <b>{val}</b>", reply_markup=await kbs.admin_panel())



# ---------------- Stats & Rescue ----------------
@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery) -> None:
    broadcasts = await db.latest_broadcasts(limit=3)
    text = (
        "<b>◉ Статистика проекта</b>\n\n"
        f"<blockquote>• <b>Всего пользователей:</b> {await db.count_all_users()}\n"
        f"• <b>Зарегистрированных:</b> {await db.count_users()}\n"
        f"• <b>Премиум пользователей:</b> {await db.count_premium_users()}\n"
        f"• <b>Бесплатных пользователей:</b> {await db.count_free_users()}\n"
        f"• <b>Администраторов:</b> {await db.count_admins()}\n"
        f"• <b>Рекламных кнопок:</b> {await db.count_ads()}\n</blockquote>"
    )

    if broadcasts:
        stats_lines = ["", "Последние рассылки:"]
        for item in broadcasts:
            stats = item.get("stats", {})
            stats_lines.append(
                f"{_format_dt(item.get('created_at'))}: {stats.get('sent', 0)} sent / "
                f"{stats.get('blocked', 0)} blocked / {stats.get('failed', 0)} failed"
            )
        text += "\n".join(stats_lines)

    await call.message.edit_text(text, reply_markup=await kbs.admin_panel())
    await call.answer()


@router.callback_query(F.data == "admin:rescue")
async def admin_rescue_panel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "<b>Rescue panel</b>\n\n"
        "Manual backups, migration export and subscription repair tools.",
        reply_markup=await kbs.admin_rescue_panel(),
    )
    await call.answer()


@router.callback_query(F.data == "admin:backup_now")
async def admin_backup_now(call: CallbackQuery) -> None:
    await call.message.edit_text("Creating database backup...")
    try:
        path = await backup_service.run_once()
    except Exception as exc:
        await call.message.edit_text(f"Backup failed: <code>{exc}</code>", reply_markup=await kbs.admin_rescue_panel())
        await call.answer()
        return
    await call.message.edit_text(
        "Backup is ready.\n"
        f"Path: <code>{path}</code>",
        reply_markup=await kbs.admin_rescue_panel(),
    )
    await call.answer("Backup ready")


@router.callback_query(F.data == "admin:export_migration")
async def admin_export_migration(call: CallbackQuery) -> None:
    await call.message.edit_text("Preparing migration export...")
    try:
        path = await export_database()
    except Exception as exc:
        await call.message.edit_text(f"Export failed: <code>{exc}</code>", reply_markup=await kbs.admin_rescue_panel())
        await call.answer()
        return
    await call.message.edit_text(
        "Migration export is ready.\n"
        f"Path: <code>{path}</code>\n\n"
        "Move this folder to the new server and import it with <code>tools/migrate_db.py</code>.",
        reply_markup=await kbs.admin_rescue_panel(),
    )
    await call.answer("Export ready")


@router.callback_query(F.data == "admin:subscription_maintenance")
async def admin_subscription_maintenance(call: CallbackQuery) -> None:
    await call.message.edit_text("Checking expired subscriptions...")
    stats = await subscription_maintenance_service.run_once()
    await call.message.edit_text(
        "<b>Subscription maintenance finished</b>\n\n"
        f"Checked: <code>{stats['checked']}</code>\n"
        f"Moved to expired config: <code>{stats['expired']}</code>\n"
        f"Errors: <code>{stats['failed']}</code>",
        reply_markup=await kbs.admin_rescue_panel(),
    )
    await call.answer("Done")


@router.callback_query(F.data == "admin:health")
async def admin_health(call: CallbackQuery) -> None:
    sections = [
        "<b>Health check</b>",
        f"Mongo users: <code>{await db.count_all_users()}</code>",
        f"PasarGuard Free Group target: <code>{settings.pasarguard_free_group_name}</code>",
        f"PasarGuard Premium Group target: <code>{settings.pasarguard_premium_group_name}</code>",
    ]

    checks = [
        ("PasarGuard /api/groups", manager.get_all_groups),
        ("PasarGuard /api/system", manager.get_system_stats),
        ("PasarGuard /api/nodes", manager.get_nodes),
        ("PasarGuard /api/inbounds", manager.get_inbounds),
    ]

    for title, loader in checks:
        try:
            sections.append(_json_block(title, await loader()))
        except Exception as exc:
            sections.append(f"<b>{escape(title)}</b>\n<code>failed: {escape(str(exc))}</code>")

    chunks = _split_health_text(sections)
    markup = await kbs.admin_rescue_panel()
    if len(chunks) == 1:
        await call.message.edit_text(chunks[0], reply_markup=markup)
    else:
        await call.message.edit_text(chunks[0])
        for chunk in chunks[1:-1]:
            await call.message.answer(chunk)
        await call.message.answer(chunks[-1], reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "admin:set_subscription_alert")
async def set_subscription_alert_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SubscriptionAlertState.waiting_for_alert)
    await call.message.edit_text(
        "Send temporary alert in format:\n"
        "<code>24 | Maintenance today. Subscription may be unstable.</code>\n\n"
        "The number is TTL in hours.",
        reply_markup=await kbs.admin_rescue_panel(),
    )
    await call.answer()


@router.message(SubscriptionAlertState.waiting_for_alert)
async def set_subscription_alert_process(message: Message, state: FSMContext) -> None:
    if "|" not in message.text:
        await message.answer("Use format: <code>24 | alert text</code>")
        return

    raw_hours, text = [part.strip() for part in message.text.split("|", 1)]
    if not raw_hours.isdigit() or int(raw_hours) < 1:
        await message.answer("TTL must be a positive number of hours.")
        return
    if len(text) < 3:
        await message.answer("Alert text is too short.")
        return
    if len(text) > 200:
        await message.answer("Telegram callback alerts support up to 200 characters. Shorten the text.")
        return

    alert = await db.set_subscription_alert(text=text, ttl_hours=int(raw_hours), created_by=message.from_user.id)
    await state.clear()
    await message.answer(
        "Subscription alert enabled.\n"
        f"Expires at: <code>{_format_dt(alert['expires_at'])}</code>",
        reply_markup=await kbs.admin_rescue_panel(),
    )


@router.callback_query(F.data == "admin:clear_subscription_alert")
async def clear_subscription_alert(call: CallbackQuery) -> None:
    await db.clear_subscription_alert()
    await call.message.edit_text("Subscription alert disabled.", reply_markup=await kbs.admin_rescue_panel())
    await call.answer("Disabled")


# ---------------- User Lookup & Actions ----------------
@router.callback_query(F.data == "admin:user_lookup")
async def admin_user_lookup(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserLookupState.waiting_for_query)
    await call.message.edit_text("Введите Telegram ID пользователя или username без @.")
    await call.answer()


@router.message(UserLookupState.waiting_for_query)
async def admin_user_lookup_process(message: Message, state: FSMContext) -> None:
    user = await db.find_user(message.text)
    if not user and not message.text.strip().isdigit():
        await message.answer("Пользователь не найден. Попробуйте еще раз.")
        return

    await state.clear()
    user_id = int(message.text.strip()) if not user else user["user_id"]
    tariff = await db.get_user_tariff(user_id)
    await message.answer(
        await _user_card(user_id),
        reply_markup=await kbs.admin_user_actions(user_id, is_premium=(tariff == "premium")),
    )


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user_actions_handler(call: CallbackQuery, state: FSMContext) -> None:
    _, action, raw_user_id = call.data.split(":")
    user_id = int(raw_user_id)
    username = db.build_vpn_username(user_id)

    if action == "block":
        await state.set_state(BlockUserState.waiting_for_reason)
        await state.update_data(block_user_id=user_id)
        await call.message.edit_text(f"Send block reason for <code>{user_id}</code>.")
        await call.answer()
        return

    if action == "set_limit":
        await state.set_state(SetUserDeviceLimitState.waiting_for_limit)
        await state.update_data(target_user_id=user_id)
        current_limit = await db.get_user_device_limit(user_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="« Назад к карточке", callback_data=f"admin_user:refresh:{user_id}"))
        await call.message.edit_text(
            f"📱 <b>Индивидуальный лимит устройств для ID {user_id}</b>\n\n"
            f"Текущий лимит: <b>{current_limit}</b>\n\n"
            "Введите число (от 1 до 50) для установки индивидуального лимита, "
            "либо отправьте <code>0</code> для сброса к тарифному умолчанию:",
            reply_markup=builder.as_markup(),
        )
        await call.answer()
        return

    try:
        tariff = await db.get_user_tariff(user_id)

        if action == "make_premium":
            now = datetime.now(timezone.utc)
            await db.set_user_tariff(user_id, "premium", now + timedelta(days=30))
            await manager.upgrade_user_to_premium(username, 30)
            await call.answer("Пользователю присвоен тариф Премиум (+30 дней)", show_alert=True)
        elif action == "make_free":
            await db.set_user_tariff(user_id, "free", None)
            await manager.downgrade_user_to_free(username, settings.trial_limit_gb, settings.trial_days)
            await call.answer("Пользователь переведен на Бесплатный тариф", show_alert=True)
        elif action == "reset_hwids":
            await manager.reset_user_hwids(username)
            await call.answer("✅ Все HWID пользователя успешно сброшены!", show_alert=True)
        elif action == "revoke_sub":
            await manager.revoke_user_subscription(username)
            await call.answer("✅ Ссылка подписки отозвана и обновлена!", show_alert=True)
        elif action == "extend30":
            await manager.extend_user_subscription(username, 30)
            await db.update_user_status(user_id, "active")
            await call.answer("Подписка продлена на 30 дней")
        elif action == "extend90":
            await manager.extend_user_subscription(username, 90)
            await db.update_user_status(user_id, "active")
            await call.answer("Подписка продлена на 90 дней")
        elif action == "activate":
            await manager.set_user_status(username, UserStatus.ACTIVE)
            await db.update_user_status(user_id, "active")
            await call.answer("Активирован")
        elif action == "disable":
            await manager.block_user(username)
            await db.update_user_status(user_id, "disabled")
            await call.answer("Отключен и перенесён в aska_blocked")
        elif action == "unblock":
            await db.clear_admin_block(user_id)
            existing = await manager.get_user_or_none(username)
            if existing:
                await manager.unblock_user(username, tariff)
            await call.answer("Разблокирован")
        elif action == "reset":
            await manager.reset_usage(username)
            await call.answer("Трафик сброшен")
        elif action != "refresh":
            await call.answer("Неизвестное действие", show_alert=True)
            return
    except MarzbanError as exc:
        await call.answer(f"Ошибка PasarGuard: {exc.detail}", show_alert=True)
        return

    new_tariff = await db.get_user_tariff(user_id)
    await call.message.edit_text(
        await _user_card(user_id),
        reply_markup=await kbs.admin_user_actions(user_id, is_premium=(new_tariff == "premium")),
    )


@router.message(BlockUserState.waiting_for_reason)
async def admin_block_user_process(message: Message, state: FSMContext) -> None:
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer("Send a longer block reason.")
        return

    data = await state.get_data()
    user_id = int(data["block_user_id"])
    username = db.build_vpn_username(user_id)
    await db.set_admin_block(user_id, reason=reason, blocked_by=message.from_user.id)
    try:
        existing = await manager.get_user_or_none(username)
        if existing:
            await manager.block_user(username)
    except MarzbanError as exc:
        await message.answer(f"Bot block saved, but PasarGuard returned an error: {exc.detail}")

    await state.clear()
    tariff = await db.get_user_tariff(user_id)
    await message.answer(
        await _user_card(user_id),
        reply_markup=await kbs.admin_user_actions(user_id, is_premium=(tariff == "premium")),
    )


@router.message(SetUserDeviceLimitState.waiting_for_limit)
async def admin_set_user_device_limit_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("target_user_id")
    await state.clear()
    if not user_id:
        await message.answer("Ошибка: пользователь не найден.")
        return

    text = message.text.strip()
    try:
        val = int(text)
        if val < 0 or val > 50:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите целое число от 0 до 50 (0 — сброс к тарифному).")
        return

    custom_limit = val if val > 0 else None
    await db.set_user_custom_device_limit(user_id, custom_limit)

    effective_limit = await db.get_user_device_limit(user_id)
    username = db.build_vpn_username(user_id)
    try:
        await manager.set_user_hwid_limit(username, effective_limit)
    except Exception as exc:
        logger.warning("Failed to sync hwid_limit for %s: %s", username, exc)

    tariff = await db.get_user_tariff(user_id)
    status_text = f"установлен на <b>{val}</b>" if val > 0 else "сброшен к тарифному по умолчанию"
    await message.answer(
        f"✅ Лимит устройств для ID <code>{user_id}</code> {status_text}.\n\n" + await _user_card(user_id),
        reply_markup=await kbs.admin_user_actions(user_id, is_premium=(tariff == "premium")),
    )



# ---------------- Admins Management ----------------
@router.callback_query(F.data == "admin:add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAdminState.waiting_for_user_id)
    await call.message.edit_text("Введите Telegram ID нового администратора.")
    await call.answer()


@router.message(AddAdminState.waiting_for_user_id)
async def add_admin_process(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Нужен числовой Telegram ID.")
        return
    admin = await db.add_admin(int(message.text.strip()))
    await state.clear()
    await message.answer(f"✓ Администратор добавлен: <code>{admin['user_id']}</code>", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data == "admin:list_admins")
async def list_admins(call: CallbackQuery) -> None:
    admins = await db.list_admins()
    if not admins:
        await call.message.edit_text("Список администраторов пуст.", reply_markup=await kbs.admin_panel())
        await call.answer()
        return
    await call.message.edit_text(
        "\n".join(["<b>• Администраторы</b>"] + [f"<code>{item['user_id']}</code> ({item.get('source', 'bot')})" for item in admins]),
        reply_markup=await kbs.admins_list(admins),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin:delete_admin:"))
async def delete_admin(call: CallbackQuery) -> None:
    _, _, raw_user_id = call.data.split(":")
    user_id = int(raw_user_id)
    if user_id in settings.admin_ids:
        await call.answer("Админ из .env не удаляется.", show_alert=True)
        return
    await db.remove_admin(user_id)
    await list_admins(call)


# ---------------- Channels & Ads Management ----------------
@router.callback_query(F.data == "admin:channels")
async def sponsor_channels(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_channels(call)
    await call.answer()


@router.callback_query(F.data == "admin:add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddChannelState.waiting_for_channel)
    await call.message.edit_text(
        "Отправьте канал в формате:\n"
        "<code>@channel_name</code>\n"
        "или\n"
        "<code>https://t.me/channel_name</code>\n\n"
        "Можно с подписью:\n"
        "<code>@channel_name | Главный спонсор</code>"
    )
    await call.answer()


@router.message(AddChannelState.waiting_for_channel)
async def add_channel_process(message: Message, state: FSMContext) -> None:
    raw_text = message.text.strip()
    channel_ref, title = raw_text, None
    if "|" in raw_text:
        channel_ref, title = [part.strip() for part in raw_text.split("|", 1)]
    channel = await db.add_channel(channel_ref, title=title)
    await state.clear()
    await message.answer(
        f"✓ Sponsor-канал добавлен:\n{channel.get('title', channel['channel_ref'])} -> <code>{channel['channel_ref']}</code>",
        reply_markup=await kbs.admin_panel(),
    )


@router.callback_query(F.data.startswith("admin:delete_channel:"))
async def delete_channel(call: CallbackQuery) -> None:
    _, _, channel_ref = call.data.split(":", 2)
    await db.remove_channel(channel_ref)
    await _show_channels(call)
    await call.answer("Канал удален")


@router.callback_query(F.data == "admin:ads")
async def ads_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_ads(call)
    await call.answer()


@router.callback_query(F.data == "admin:add_ad")
async def add_ad_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAdState.waiting_for_ad)
    await call.message.edit_text("Отправьте рекламо-кнопку в формате:\n<code>Текст | https://site.com</code>")
    await call.answer()


@router.message(AddAdState.waiting_for_ad)
async def add_ad_process(message: Message, state: FSMContext) -> None:
    if "|" not in message.text:
        await message.answer("Нужен формат: Текст | https://site.com")
        return
    text, url = [part.strip() for part in message.text.split("|", 1)]
    if not url.startswith(("http://", "https://")):
        await message.answer("URL должен начинаться с http:// или https://")
        return
    await db.add_ad(text, url)
    await state.clear()
    await message.answer("✓ Рекламная кнопка добавлена.", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data.startswith("admin:delete_ad:"))
async def delete_ad(call: CallbackQuery) -> None:
    _, _, ad_id = call.data.split(":")
    await db.delete_ad(ad_id)
    await _show_ads(call)
    await call.answer("Рекламная кнопка удалена")


# ---------------- Broadcasts ----------------
@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BroadcastState.waiting_for_text)
    await call.message.edit_text("Введите текст рассылки. HTML-разметка поддерживается.")
    await call.answer()


@router.message(BroadcastState.waiting_for_text)
async def broadcast_text_received(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text, broadcast_buttons=[])
    await state.set_state(BroadcastState.ready)
    await _send_broadcast_preview(message, state)


@router.callback_query(F.data == "broadcast:add_button")
async def broadcast_add_button(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("broadcast_text"):
        await call.answer("Сначала задайте текст рассылки.", show_alert=True)
        return
    await state.set_state(BroadcastState.waiting_for_button)
    await call.message.answer("Отправьте кнопку в формате:\n<code>Текст | https://site.com</code>\nили\n<code>Текст | callback:user:connect</code>")
    await call.answer()


@router.message(BroadcastState.waiting_for_button)
async def broadcast_button_received(message: Message, state: FSMContext) -> None:
    if "|" not in message.text:
        await message.answer("Нужен формат: Текст | значение")
        return
    text, value = [part.strip() for part in message.text.split("|", 1)]
    button_type = "url"
    if value.startswith("callback:"):
        button_type = "callback"
        value = value.split(":", 1)[1].strip()
    elif not value.startswith(("http://", "https://")):
        await message.answer("Ссылка должна начинаться с http:// или https://, либо используйте callback:")
        return

    data = await state.get_data()
    buttons = list(data.get("broadcast_buttons", []))
    buttons.append({"text": text, "type": button_type, "value": value, "style": "primary"})
    await state.update_data(broadcast_buttons=buttons)
    await state.set_state(BroadcastState.ready)
    await _send_broadcast_preview(message, state)


@router.callback_query(F.data == "broadcast:clear_buttons")
async def broadcast_clear_buttons(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(broadcast_buttons=[])
    await call.message.answer("• Кнопки очищены.")
    await _send_broadcast_preview(call.message, state)
    await call.answer()


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("✕ Создание рассылки отменено.", reply_markup=await kbs.admin_panel())
    await call.answer()


@router.callback_query(F.data.in_({"broadcast:send_all", "broadcast:send_active"}))
async def broadcast_send(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("broadcast_text"):
        await call.answer("Нет текста рассылки.", show_alert=True)
        return
    await state.clear()
    status_message = await call.message.answer("⋯ Запускаю рассылку...")
    stats = await broadcast_service.send_broadcast(
        bot=call.bot,
        text=data["broadcast_text"],
        buttons=data.get("broadcast_buttons", []),
        active_only=call.data == "broadcast:send_active",
        status_message=status_message,
    )
    total = stats["sent"] + stats["failed"] + stats["blocked"]
    await status_message.edit_text(
        "✓ Рассылка завершена.\n"
        f"Отправлено: {stats['sent']}\n"
        f"Заблокировали бота: {stats['blocked']}\n"
        f"Ошибки: {stats['failed']}\n"
        f"Всего обработано: {total}"
    )
    await call.message.answer("Рассылка завершена")


# ---------------- Support tickets handling ----------------
@router.callback_query(F.data.startswith("support:answer:"))
async def support_answer_start(call: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_user_id, ticket_id = call.data.split(":")
    await state.set_state(SupportReplyState.waiting_for_text)
    await state.update_data(to_user=int(raw_user_id), ticket_id=ticket_id)
    await call.message.answer("Введите ответ пользователю:")
    await call.answer()


@router.callback_query(F.data.startswith("support:decline:"))
async def support_decline_start(call: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_user_id, ticket_id = call.data.split(":")
    await state.set_state(SupportDeclineState.waiting_for_text)
    await state.update_data(to_user=int(raw_user_id), ticket_id=ticket_id)
    await call.message.answer("Введите причину отказа:")
    await call.answer()


@router.message(SupportReplyState.waiting_for_text)
async def support_answer_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.bot.send_message(
        data["to_user"],
        f"• Поддержка ответила по обращению #{data['ticket_id']}\n\n<code>{message.text}</code>",
    )
    await db.close_support_ticket(data["ticket_id"], "answered")
    await state.clear()
    await message.answer("Ответ отправлен.", reply_markup=await kbs.admin_panel())


@router.message(SupportDeclineState.waiting_for_text)
async def support_decline_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.bot.send_message(
        data["to_user"],
        f"✕ Обращение #{data['ticket_id']} отклонено.\n\nПричина:\n{message.text}",
    )
    await db.close_support_ticket(data["ticket_id"], "declined")
    await state.clear()
    await message.answer("Пользователь уведомлен.", reply_markup=await kbs.admin_panel())


# ---------------- Refund Requests Management ----------------
@router.callback_query(F.data == "admin:refund_requests")
async def admin_refund_requests_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    requests = await db.get_pending_refund_requests(limit=20)
    if not requests:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="« В админ-панель", callback_data="admin:panel"))
        await call.message.edit_text(
            "<b>↺ Запросы на отмену платежей</b>\n\n"
            "» Нет активных запросов на отмену платежей.",
            reply_markup=builder.as_markup(),
        )
        await call.answer()
        return

    lines = [
        "<b>↺ Запросы на отмену платежей</b>",
        "",
        f"Ожидают решения: <b>{len(requests)}</b>",
        "",
        "<blockquote>",
    ]
    for req in requests:
        order_id = req.get("order_id", "")
        amount = req.get("amount", 0.0)
        user_id = req.get("user_id", "")
        dt_val = req.get("refund_requested_at") or req.get("created_at")
        dt_str = _format_dt(dt_val)
        lines.append(f"• <b>{amount:.2f} ₽</b> (ID <code>{user_id}</code>) — #{order_id[-6:]} ({dt_str})")
    lines.extend([
        "</blockquote>",
        "",
        "Выберите заявку для просмотра и принятия решения:",
    ])
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=await kbs.admin_refund_requests_list_keyboard(requests),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_refund:view:"))
async def admin_refund_view_order(call: CallbackQuery) -> None:
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    user_id = order.get("user_id")
    local_user = await db.get_user_by_id(user_id) if user_id else None
    user_tag = f"@{local_user.get('username')}" if local_user and local_user.get("username") else f"ID {user_id}"
    balance = await db.get_user_balance(user_id) if user_id else 0.0

    status_str = order.get("status", "unknown")
    if status_str == "refund_requested":
        status_label = "⋯ Ожидает решения администратора"
    elif status_str == "refunded":
        status_label = "↺ Одобрен (средства возвращены)"
    elif status_str == "refund_declined":
        status_label = "✕ Отклонен администратором"
    elif status_str == "paid":
        status_label = "✓ Оплачен"
    else:
        status_label = status_str

    created_str = _format_dt(order.get("created_at"))
    req_str = _format_dt(order.get("refund_requested_at"))

    type_map = {
        "topup": "Пополнение баланса",
        "tariff": f"Оплата тарифа ({order.get('days', 30)} дн.)",
        "balance_tariff": f"Оплата тарифа с баланса ({order.get('days', 30)} дн.)",
    }
    type_str = type_map.get(order.get("order_type"), order.get("order_type", "Платеж"))

    lines = [
        "<b>↺ Карточка запроса на отмену платежа</b>",
        "",
        f"<blockquote>Заказ: <code>{order_id}</code>\n"
        f"Пользователь: <code>{user_id}</code> ({user_tag})\n"
        f"Текущий баланс: <b>{balance:.2f} ₽</b>\n"
        f"Тип платежа: <b>{type_str}</b>\n"
        f"Сумма: <b>{order.get('amount', 0):.2f} ₽</b>\n"
        f"Дата заказа: <code>{created_str}</code>\n"
        f"Запрос отмены: <code>{req_str}</code>\n"
        f"Статус: <b>{status_label}</b>\n"
        f"Причина отмены: <i>{order.get('refund_reason', 'Не указана')}</i></blockquote>",
    ]

    if status_str == "refund_requested":
        lines.append("\nВыберите решение по заявке:")
        reply_markup = await kbs.admin_refund_action_keyboard(order_id)
    else:
        reply_markup = await kbs.admin_refund_done_keyboard()

    await call.message.edit_text("\n".join(lines), reply_markup=reply_markup)
    await call.answer()


@router.callback_query(F.data.startswith("admin_refund:approve:"))
async def admin_refund_approve(call: CallbackQuery) -> None:
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    updated = await db.resolve_payment_refund(order_id, approve=True, admin_id=call.from_user.id)
    if not updated:
        await call.answer("Заявка уже была обработана ранее или не ожидает решения.", show_alert=True)
        return

    user_id = order["user_id"]
    amount = float(order.get("amount", 0.0))
    order_type = order.get("order_type", "topup")

    # Handle refund depending on order type
    if order_type == "topup":
        await db.change_user_balance(user_id, -amount)
    elif order_type == "balance_tariff":
        await db.change_user_balance(user_id, amount)
        await db.set_user_tariff(user_id, "free", None)
        username = db.build_vpn_username(user_id)
        try:
            await manager.downgrade_user_to_free(
                username,
                data_limit_gb=settings.default_data_limit_gb,
                expire_days=settings.default_expire_days,
            )
        except Exception as exc:
            logger.warning("Failed to downgrade user %s in PasarGuard: %s", username, exc)
    elif order_type == "tariff":
        await db.set_user_tariff(user_id, "free", None)
        username = db.build_vpn_username(user_id)
        try:
            await manager.downgrade_user_to_free(
                username,
                data_limit_gb=settings.default_data_limit_gb,
                expire_days=settings.default_expire_days,
            )
        except Exception as exc:
            logger.warning("Failed to downgrade user %s in PasarGuard: %s", username, exc)

    # Notify user
    try:
        user_notify_text = (
            "✓ <b>Запрос на отмену платежа одобрен</b>\n\n"
            f"<blockquote>Заказ: <code>{order_id}</code>\n"
            f"Сумма: <b>{amount:.2f} ₽</b>\n"
            f"Статус: <b>Возврат подтверждён</b></blockquote>\n\n"
            "Средства возвращены в соответствии с регламентом сервиса."
        )
        await call.bot.send_message(user_id, user_notify_text)
    except Exception:
        pass

    await call.message.edit_text(
        f"✓ <b>Запрос на отмену {order_id} ОДОБРЕН</b>\n\n"
        f"<blockquote>Пользователь: <code>{user_id}</code>\n"
        f"Сумма: <b>{amount:.2f} ₽</b> ({order_type})\n"
        f"Администратор: <code>{call.from_user.id}</code></blockquote>",
        reply_markup=await kbs.admin_refund_done_keyboard(),
    )
    await call.answer("Отмена платежа одобрена!")


@router.callback_query(F.data.startswith("admin_refund:decline:"))
async def admin_refund_decline(call: CallbackQuery) -> None:
    order_id = call.data.split(":", 2)[2]
    order = await db.get_payment_order(order_id)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    updated = await db.resolve_payment_refund(order_id, approve=False, admin_id=call.from_user.id)
    if not updated:
        await call.answer("Заявка уже была обработана ранее или не ожидает решения.", show_alert=True)
        return

    user_id = order["user_id"]
    amount = float(order.get("amount", 0.0))

    try:
        user_notify_text = (
            "✕ <b>В отмене платежа отказано</b>\n\n"
            f"<blockquote>Заказ: <code>{order_id}</code>\n"
            f"Сумма: <b>{amount:.2f} ₽</b>\n"
            f"Статус: <b>Запрос отклонён администратором</b></blockquote>\n\n"
            "Если у вас остались вопросы, обратитесь в службу поддержки через меню бота."
        )
        await call.bot.send_message(user_id, user_notify_text)
    except Exception:
        pass

    await call.message.edit_text(
        f"✕ <b>Запрос на отмену {order_id} ОТКЛОНЁН</b>\n\n"
        f"<blockquote>Пользователь: <code>{user_id}</code>\n"
        f"Сумма: <b>{amount:.2f} ₽</b>\n"
        f"Администратор: <code>{call.from_user.id}</code></blockquote>",
        reply_markup=await kbs.admin_refund_done_keyboard(),
    )
    await call.answer("Отмена платежа отклонена.")

