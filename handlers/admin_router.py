from __future__ import annotations

from datetime import datetime
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from config import settings
from core.app_core import MarzbanError, UserStatus, manager
from database.db import db
from database.migration import export_database
from filters import AdminFilter
from services import broadcast_service
from services.backups import backup_service
from services.subscriptions import subscription_maintenance_service
from utils import kbs

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


def _format_dt(value: datetime | None) -> str:
    if not value:
        return "n/a"
    return value.strftime("%d.%m.%Y %H:%M")


def _broadcast_buttons_text(buttons: list[dict[str, str]]) -> str:
    if not buttons:
        return "Кнопки: не добавлены"
    return "Кнопки:\n" + "\n".join(f"{index}. {button['text']} -> {button['value']}" for index, button in enumerate(buttons, start=1))


async def _user_card(user_id: int) -> str:
    user = await db.get_user_by_id(user_id)
    username = db.build_vpn_username(user_id)
    vpn_status = "Профиль еще не создан"
    traffic = "n/a"
    expire = "n/a"

    try:
        vpn_user = await manager.get_user_or_none(username)
        if vpn_user:
            traffic_limit = vpn_user.get("data_limit", 0)
            vpn_status = vpn_user.get("status", "unknown")
            traffic = (
                f"{manager.format_bytes(vpn_user.get('used_traffic', 0))} / "
                f"{manager.format_bytes(traffic_limit) if traffic_limit else 'Безлимит'}"
            )
            expire = datetime.fromtimestamp(vpn_user["expire"]).strftime("%d.%m.%Y %H:%M") if vpn_user.get("expire") else "Без срока"
    except MarzbanError as exc:
        vpn_status = f"Ошибка Marzban: {exc.detail}"

    if not user:
        return (
            "<b>👤 Пользователь не найден в боте</b>\n\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"VPN username: <code>{username}</code>\n"
            f"VPN статус: {vpn_status}\n"
            f"Трафик: {traffic}\n"
            f"Подписка до: {expire}"
        )

    lines = [
        "<b>👤 Карточка пользователя</b>",
        "",
        f"Telegram ID: <code>{user['user_id']}</code>",
    ]
    if user.get("username"):
        lines.append(f"Username: @{user['username']}")
    lines.extend(
        [
            f"Имя: {user.get('full_name') or 'n/a'}",
            f"Статус в боте: {user.get('status', 'active')}",
            f"Зарегистрирован: {_format_dt(user.get('registered_at') or user.get('created_at'))}",
            f"Sponsor-check: {'да' if user.get('is_subscribed_to_sponsors') else 'нет'}",
            f"VPN username: <code>{username}</code>",
            f"VPN статус: {vpn_status}",
            f"Трафик: {traffic}",
            f"Подписка до: {expire}",
        ]
    )
    if user.get("blocked_reason"):
        lines.append(f"Block reason: {user['blocked_reason']}")
    return "\n".join(lines)


async def _show_channels(call: CallbackQuery) -> None:
    channels = await db.list_channels()
    if not channels:
        await call.message.edit_text("📣 Sponsor-каналы пока не добавлены.", reply_markup=await kbs.sponsor_channels_list(channels))
        return

    lines = ["<b>📣 Sponsor-каналы</b>"]
    for channel in channels:
        lines.append(f"{channel.get('title', channel['channel_ref'])} -> <code>{channel['channel_ref']}</code>")
    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.sponsor_channels_list(channels))


async def _show_ads(call: CallbackQuery) -> None:
    ads = await db.list_ads()
    if not ads:
        await call.message.edit_text("🗞 Рекламные кнопки еще не добавлены.", reply_markup=await kbs.ads_list_keyboard(ads))
        return

    lines = ["<b>🗞 Рекламные кнопки</b>"]
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

    await message.answer("<b>📨 Превью рассылки</b>")
    await message.answer(text, reply_markup=kbs.build_inline_markup(buttons))
    await message.answer(_broadcast_buttons_text(buttons), reply_markup=await kbs.broadcast_menu(buttons))


@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛡 Админ-панель", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data == "admin:panel")
async def admin_panel_page(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("🛡 Админ-панель", reply_markup=await kbs.admin_panel())
    await call.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery) -> None:
    broadcasts = await db.latest_broadcasts(limit=3)
    text = (
        "<b>📊 Статистика проекта</b>\n\n"
        f"<blockquote> 👤 <b>Пользователей в базе:</b> {await db.count_all_users()}\n"
        f"✅ <b>Зарегистрированных:</b> {await db.count_users()}\n"
        f"🧌 <b>Администраторов:</b> {await db.count_admins()}\n"
        f"💠 <b>Рекламных кнопок:</b> {await db.count_ads()}\n</blockquote>"
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
    lines = ["<b>Health check</b>", ""]
    lines.append(f"Mongo users: <code>{await db.count_all_users()}</code>")
    try:
        stats = await manager.get_system_stats()
        lines.append("Marzban API: <code>ok</code>")
        if stats:
            lines.append(f"Marzban stats keys: <code>{', '.join(sorted(stats.keys())[:8])}</code>")
    except Exception as exc:
        lines.append(f"Marzban API: <code>failed: {exc}</code>")

    try:
        nodes = await manager.get_nodes()
        lines.append(f"Nodes: <code>{len(nodes)}</code>")
        for node in nodes[:10]:
            name = node.get("name") or node.get("address") or node.get("id") or "unknown"
            status = node.get("status") or node.get("message") or "unknown"
            lines.append(f"- <code>{name}</code>: {status}")
    except Exception as exc:
        lines.append(f"Nodes: <code>failed: {exc}</code>")

    await call.message.edit_text("\n".join(lines), reply_markup=await kbs.admin_rescue_panel())
    await call.answer()


@router.callback_query(F.data == "admin:set_subscription_alert")
async def set_subscription_alert_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SubscriptionAlertState.waiting_for_alert)
    await call.message.edit_text(
        "Send temporary alert in format:\n"
        "<code>24 | Maintenance today. Subscription may be unstable.</code>\n\n"
        "The number is TTL in hours. It will be shown only when user presses Subscription.",
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
    await message.answer(await _user_card(user_id), reply_markup=await kbs.admin_user_actions(user_id))


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user_actions(call: CallbackQuery, state: FSMContext) -> None:
    _, action, raw_user_id = call.data.split(":")
    user_id = int(raw_user_id)
    username = db.build_vpn_username(user_id)

    if action == "block":
        await state.set_state(BlockUserState.waiting_for_reason)
        await state.update_data(block_user_id=user_id)
        await call.message.edit_text(f"Send block reason for <code>{user_id}</code>.")
        await call.answer()
        return

    try:
        if action in {"extend30", "extend90", "activate"}:
            await manager.ensure_subscription_user(
                username=username,
                data_limit_gb=settings.trial_limit_gb,
                expire_days=settings.trial_days,
                note=f"Telegram user {user_id}",
            )

        if action == "extend30":
            await manager.extend_user_subscription(username, 30)
            await db.update_user_status(user_id, "active")
        elif action == "extend90":
            await manager.extend_user_subscription(username, 90)
            await db.update_user_status(user_id, "active")
        elif action == "activate":
            await manager.set_user_status(username, UserStatus.ACTIVE)
            await db.update_user_status(user_id, "active")
        elif action == "disable":
            await manager.set_user_status(username, UserStatus.DISABLED)
            await db.update_user_status(user_id, "disabled")
        elif action == "unblock":
            await db.clear_admin_block(user_id)
            existing = await manager.get_user_or_none(username)
            if existing:
                await manager.set_user_status(username, UserStatus.ACTIVE)
        elif action == "reset":
            await manager.reset_usage(username)
        elif action != "refresh":
            await call.answer("Неизвестное действие", show_alert=True)
            return
    except MarzbanError as exc:
        await call.answer(f"Ошибка Marzban: {exc.detail}", show_alert=True)
        return

    await call.message.edit_text(await _user_card(user_id), reply_markup=await kbs.admin_user_actions(user_id))
    await call.answer("Готово")


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
            await manager.set_user_status(username, UserStatus.DISABLED)
    except MarzbanError as exc:
        await message.answer(f"Bot block saved, but Marzban returned an error: {exc.detail}")

    await state.clear()
    await message.answer(await _user_card(user_id), reply_markup=await kbs.admin_user_actions(user_id))


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
    await message.answer(f"✅ Администратор добавлен: <code>{admin['user_id']}</code>", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data == "admin:list_admins")
async def list_admins(call: CallbackQuery) -> None:
    admins = await db.list_admins()
    if not admins:
        await call.message.edit_text("Список администраторов пуст.", reply_markup=await kbs.admin_panel())
        await call.answer()
        return
    await call.message.edit_text(
        "\n".join(["<b>🛡 Администраторы</b>"] + [f"<code>{item['user_id']}</code> ({item.get('source', 'bot')})" for item in admins]),
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
        f"✅ Sponsor-канал добавлен:\n{channel.get('title', channel['channel_ref'])} -> <code>{channel['channel_ref']}</code>",
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
    await message.answer("✅ Рекламная кнопка добавлена.", reply_markup=await kbs.admin_panel())


@router.callback_query(F.data.startswith("admin:delete_ad:"))
async def delete_ad(call: CallbackQuery) -> None:
    _, _, ad_id = call.data.split(":")
    await db.delete_ad(ad_id)
    await _show_ads(call)
    await call.answer("Рекламная кнопка удалена")


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
    await call.message.answer("Отправьте кнопку в формате:\n<code>Текст | https://site.com</code>\nили\n<code>Текст | callback:user:sub</code>")
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
    await call.message.answer("🧹 Кнопки очищены.")
    await _send_broadcast_preview(call.message, state)
    await call.answer()


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("❌ Создание рассылки отменено.", reply_markup=await kbs.admin_panel())
    await call.answer()


@router.callback_query(F.data.in_({"broadcast:send_all", "broadcast:send_active"}))
async def broadcast_send(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("broadcast_text"):
        await call.answer("Нет текста рассылки.", show_alert=True)
        return
    await state.clear()
    status_message = await call.message.answer("🚀 Запускаю рассылку...")
    stats = await broadcast_service.send_broadcast(
        bot=call.bot,
        text=data["broadcast_text"],
        buttons=data.get("broadcast_buttons", []),
        active_only=call.data == "broadcast:send_active",
        status_message=status_message,
    )
    total = stats["sent"] + stats["failed"] + stats["blocked"]
    await status_message.edit_text(
        "✅ Рассылка завершена.\n"
        f"Отправлено: {stats['sent']}\n"
        f"Заблокировали бота: {stats['blocked']}\n"
        f"Ошибки: {stats['failed']}\n"
        f"Всего обработано: {total}"
    )
    await call.message.answer("Рассылка завершена")


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
        f"⚡️ Поддержка ответила по обращению #{data['ticket_id']}\n\n<code>{message.text}</code>",
    )
    await db.close_support_ticket(data["ticket_id"], "answered")
    await state.clear()
    await message.answer("Ответ отправлен.", reply_markup=await kbs.admin_panel())


@router.message(SupportDeclineState.waiting_for_text)
async def support_decline_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.bot.send_message(
        data["to_user"],
        f"❌ Обращение #{data['ticket_id']} отклонено.\n\nПричина:\n{message.text}",
    )
    await db.close_support_ticket(data["ticket_id"], "declined")
    await state.clear()
    await message.answer("Пользователь уведомлен.", reply_markup=await kbs.admin_panel())
