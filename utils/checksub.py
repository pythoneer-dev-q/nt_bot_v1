from __future__ import annotations

import logging
from dataclasses import dataclass
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from config import settings
from database.db import db

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SubscriptionCheckResult:
    is_subscribed: bool
    missing_channels: list[dict]
    unavailable_channels: list[dict]


async def check_sub(user_id: int, message: Message) -> SubscriptionCheckResult:
    channels = await db.list_channels()
    if not channels and settings.required_channels:
        channels = [{"channel_ref": item, "title": item, "url": None} for item in settings.required_channels]

    if not channels:
        return SubscriptionCheckResult(is_subscribed=True, missing_channels=[], unavailable_channels=[])

    missing_channels: list[dict] = []
    unavailable_channels: list[dict] = []

    for channel in channels:
        channel_ref = channel["channel_ref"]
        try:
            member = await message.bot.get_chat_member(channel_ref, user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("Sponsor channel %s is unavailable for membership check: %s", channel_ref, exc)
            unavailable_channels.append(channel)
            continue

        if member.status not in {"member", "administrator", "creator"}:
            missing_channels.append(channel)

    return SubscriptionCheckResult(
        is_subscribed=not missing_channels,
        missing_channels=missing_channels,
        unavailable_channels=unavailable_channels,
    )
