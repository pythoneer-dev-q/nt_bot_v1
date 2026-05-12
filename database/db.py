from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from random import choice
from uuid import uuid4
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, IndexModel, ReturnDocument
from config import settings
from config.settings import normalize_channel_ref


def _channel_url(channel_ref: str) -> str | None:
    if channel_ref.startswith("@"):
        return f"https://t.me/{channel_ref[1:]}"
    return None


class MongoStorage:
    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.database: AsyncIOMotorDatabase | None = None
        self.users: AsyncIOMotorCollection | None = None
        self.admins: AsyncIOMotorCollection | None = None
        self.broadcasts: AsyncIOMotorCollection | None = None
        self.channels: AsyncIOMotorCollection | None = None
        self.ads: AsyncIOMotorCollection | None = None
        self.support_tickets: AsyncIOMotorCollection | None = None
        self.backup_runs: AsyncIOMotorCollection | None = None
        self.alerts: AsyncIOMotorCollection | None = None

    async def connect(self) -> None:
        if self.client is not None:
            return

        self.client = AsyncIOMotorClient(settings.mongo_dsn, maxPoolSize=200, minPoolSize=5)
        self.database = self.client[settings.mongo_db]
        self.users = self.database["users"]
        self.admins = self.database["admins"]
        self.broadcasts = self.database["broadcasts"]
        self.channels = self.database["channels"]
        self.ads = self.database["ads"]
        self.support_tickets = self.database["support_tickets"]
        self.backup_runs = self.database["backup_runs"]
        self.alerts = self.database["alerts"]

    async def close(self) -> None:
        if self.client is None:
            return

        self.client.close()
        self.client = None
        self.database = None
        self.users = None
        self.admins = None
        self.broadcasts = None
        self.channels = None
        self.ads = None
        self.support_tickets = None
        self.backup_runs = None
        self.alerts = None

    async def ensure_indexes(self) -> None:
        assert self.users is not None
        assert self.admins is not None
        assert self.broadcasts is not None
        assert self.channels is not None
        assert self.ads is not None
        assert self.support_tickets is not None
        assert self.backup_runs is not None
        assert self.alerts is not None

        await self.users.create_indexes(
            [
                IndexModel([("user_id", ASCENDING)], unique=True),
                IndexModel([("username", ASCENDING)], unique=False),
                IndexModel([("vpn_username", ASCENDING)], unique=True),
                IndexModel([("status", ASCENDING)], unique=False),
                IndexModel([("is_registered", ASCENDING)], unique=False),
                IndexModel([("blocked_at", ASCENDING)], unique=False),
            ]
        )
        await self.admins.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True)])
        await self.broadcasts.create_indexes([IndexModel([("created_at", ASCENDING)], unique=False)])
        await self.channels.create_indexes([IndexModel([("channel_ref", ASCENDING)], unique=True)])
        await self.ads.create_indexes([IndexModel([("created_at", ASCENDING)], unique=False)])
        await self.backup_runs.create_indexes([IndexModel([("created_at", ASCENDING)], unique=False)])
        await self.alerts.create_indexes(
            [
                IndexModel([("scope", ASCENDING), ("is_active", ASCENDING)], unique=False),
                IndexModel([("expires_at", ASCENDING)], unique=False),
            ]
        )
        await self.support_tickets.create_indexes(
            [
                IndexModel([("ticket_id", ASCENDING)], unique=True),
                IndexModel([("user_id", ASCENDING)], unique=False),
                IndexModel([("status", ASCENDING)], unique=False),
            ]
        )

        if settings.admin_ids:
            for admin_id in settings.admin_ids:
                await self.admins.update_one(
                    {"user_id": admin_id},
                    {
                        "$setOnInsert": {
                            "user_id": admin_id,
                            "source": "env",
                            "created_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )

        for channel_ref in settings.required_channels:
            await self.add_channel(channel_ref, source="env")

    async def ensure_user(self, user_id: int, username: str | None, full_name: str | None) -> dict:
        assert self.users is not None
        now = datetime.now(timezone.utc)
        existing = await self.users.find_one({"user_id": user_id})
        next_status = existing.get("status", "active") if existing else "active"
        return await self.users.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "username": username,
                    "full_name": full_name,
                    "vpn_username": self.build_vpn_username(user_id),
                    "status": next_status,
                    "is_bot_blocked": False,
                    "last_seen_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": uuid4().hex,
                    "user_id": user_id,
                    "is_registered": False,
                    "is_subscribed_to_sponsors": False,
                    "accepted_rules_at": None,
                    "last_free_renew_at": None,
                    "blocked_at": None,
                    "blocked_by": None,
                    "blocked_reason": None,
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def mark_rules_accepted(self, user_id: int) -> None:
        assert self.users is not None
        now = datetime.now(timezone.utc)
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "accepted_rules_at": now,
                    "updated_at": now,
                }
            },
        )

    async def mark_user_registered(self, user_id: int, subscribed: bool) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "is_registered": True,
                    "is_subscribed_to_sponsors": subscribed,
                    "registered_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def set_user_subscription_state(self, user_id: int, subscribed: bool) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "is_subscribed_to_sponsors": subscribed,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def get_user_by_id(self, user_id: int) -> dict | None:
        assert self.users is not None
        return await self.users.find_one({"user_id": user_id})

    async def mark_free_renewal(self, user_id: int) -> None:
        assert self.users is not None
        now = datetime.now(timezone.utc)
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"last_free_renew_at": now, "updated_at": now}},
        )

    async def find_user(self, query: str) -> dict | None:
        assert self.users is not None
        normalized = query.strip().lstrip("@")
        if normalized.isdigit():
            found = await self.users.find_one({"user_id": int(normalized)})
            if found:
                return found
        return await self.users.find_one({"username": normalized})

    async def update_user_status(self, user_id: int, status: str) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
        )

    async def set_admin_block(self, user_id: int, reason: str, blocked_by: int) -> None:
        assert self.users is not None
        now = datetime.now(timezone.utc)
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "status": "admin_blocked",
                    "blocked_reason": reason.strip(),
                    "blocked_by": blocked_by,
                    "blocked_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )

    async def clear_admin_block(self, user_id: int) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "status": "active",
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {
                    "blocked_reason": "",
                    "blocked_by": "",
                    "blocked_at": "",
                },
            },
        )

    async def is_admin_blocked(self, user_id: int) -> tuple[bool, str | None]:
        user = await self.get_user_by_id(user_id)
        if not user:
            return False, None
        return user.get("status") == "admin_blocked", user.get("blocked_reason")

    async def mark_user_blocked(self, user_id: int, blocked: bool) -> None:
        assert self.users is not None
        existing = await self.users.find_one({"user_id": user_id})
        if existing and existing.get("status") == "admin_blocked":
            status = "admin_blocked"
        else:
            status = "blocked" if blocked else "active"
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "is_bot_blocked": blocked,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def touch_broadcast_delivery(self, user_id: int) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"last_broadcast_at": datetime.now(timezone.utc)}},
        )

    async def iter_users(self, active_only: bool = False) -> AsyncIterator[dict]:
        assert self.users is not None
        query: dict = {"is_registered": True}
        if active_only:
            query["status"] = {"$nin": ["blocked", "admin_blocked"]}
        cursor = self.users.find(query, projection={"_id": 0})
        async for user in cursor:
            yield user

    async def count_users(self, active_only: bool = False) -> int:
        assert self.users is not None
        query: dict = {"is_registered": True}
        if active_only:
            query["status"] = {"$nin": ["blocked", "admin_blocked"]}
        return await self.users.count_documents(query)

    async def iter_registered_users(self) -> AsyncIterator[dict]:
        assert self.users is not None
        cursor = self.users.find({"is_registered": True}, projection={"_id": 0, "user_id": 1})
        async for user in cursor:
            yield user

    async def mark_subscription_expired(self, user_id: int) -> None:
        assert self.users is not None
        await self.users.update_one(
            {"user_id": user_id, "status": {"$ne": "admin_blocked"}},
            {
                "$set": {
                    "status": "subscription_expired",
                    "subscription_expired_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    async def record_backup_run(self, path: str, collections: list[str], ok: bool, error: str | None = None) -> None:
        assert self.backup_runs is not None
        await self.backup_runs.insert_one(
            {
                "_id": uuid4().hex,
                "path": path,
                "collections": collections,
                "ok": ok,
                "error": error,
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def set_subscription_alert(self, text: str, ttl_hours: int, created_by: int) -> dict:
        assert self.alerts is not None
        now = datetime.now(timezone.utc)
        document = {
            "_id": uuid4().hex,
            "scope": "subscription",
            "text": text.strip(),
            "is_active": True,
            "created_by": created_by,
            "created_at": now,
            "expires_at": now + timedelta(hours=ttl_hours),
        }
        await self.alerts.update_many(
            {"scope": "subscription", "is_active": True},
            {"$set": {"is_active": False, "disabled_at": now}},
        )
        await self.alerts.insert_one(document)
        return document

    async def clear_subscription_alert(self) -> None:
        assert self.alerts is not None
        await self.alerts.update_many(
            {"scope": "subscription", "is_active": True},
            {"$set": {"is_active": False, "disabled_at": datetime.now(timezone.utc)}},
        )

    async def get_active_subscription_alert(self) -> dict | None:
        assert self.alerts is not None
        now = datetime.now(timezone.utc)
        alert = await self.alerts.find_one(
            {"scope": "subscription", "is_active": True, "expires_at": {"$gt": now}},
            sort=[("created_at", -1)],
        )
        if alert:
            return alert
        await self.alerts.update_many(
            {"scope": "subscription", "is_active": True, "expires_at": {"$lte": now}},
            {"$set": {"is_active": False, "disabled_at": now}},
        )
        return None

    async def count_all_users(self) -> int:
        assert self.users is not None
        return await self.users.count_documents({})

    async def is_admin(self, user_id: int) -> bool:
        if user_id in settings.admin_ids:
            return True
        assert self.admins is not None
        return await self.admins.find_one({"user_id": user_id}) is not None

    async def add_admin(self, user_id: int) -> dict:
        assert self.admins is not None
        payload = {"user_id": user_id, "source": "bot", "created_at": datetime.now(timezone.utc)}
        await self.admins.update_one({"user_id": user_id}, {"$setOnInsert": payload}, upsert=True)
        return await self.admins.find_one({"user_id": user_id})

    async def remove_admin(self, user_id: int) -> None:
        assert self.admins is not None
        if user_id in settings.admin_ids:
            return
        await self.admins.delete_one({"user_id": user_id})

    async def list_admins(self) -> list[dict]:
        assert self.admins is not None
        return await self.admins.find({"user_id": {"$exists": True}}).sort("user_id", ASCENDING).to_list(length=100)

    async def count_admins(self) -> int:
        assert self.admins is not None
        return await self.admins.count_documents({"user_id": {"$exists": True}})

    async def list_support_recipient_ids(self) -> list[int]:
        # Поддержку обрабатывают админы, чтобы не плодить параллельные роли в этой версии проекта.
        admin_ids = {item["user_id"] for item in await self.list_admins()}
        admin_ids.update(settings.admin_ids)
        return sorted(admin_ids)

    async def add_channel(self, raw_channel_ref: str, title: str | None = None, source: str = "bot") -> dict:
        assert self.channels is not None
        channel_ref = normalize_channel_ref(raw_channel_ref)
        payload = {
            "channel_ref": channel_ref,
            "title": title or channel_ref,
            "url": _channel_url(channel_ref),
            "source": source,
            "is_active": True,
            "updated_at": datetime.now(timezone.utc),
        }
        await self.channels.update_one(
            {"channel_ref": channel_ref},
            {
                "$set": payload,
                "$setOnInsert": {"_id": uuid4().hex, "created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )
        return await self.channels.find_one({"channel_ref": channel_ref})

    async def remove_channel(self, channel_ref: str) -> None:
        assert self.channels is not None
        await self.channels.delete_one({"channel_ref": normalize_channel_ref(channel_ref)})

    async def list_channels(self) -> list[dict]:
        assert self.channels is not None
        return await self.channels.find({"is_active": True}).sort("created_at", ASCENDING).to_list(length=100)

    async def count_channels(self) -> int:
        assert self.channels is not None
        return await self.channels.count_documents({"is_active": True})

    async def add_ad(self, text: str, url: str) -> dict:
        assert self.ads is not None
        document = {
            "_id": uuid4().hex[:12],
            "text": text,
            "url": url,
            "created_at": datetime.now(timezone.utc),
        }
        await self.ads.insert_one(document)
        return document

    async def delete_ad(self, ad_id: str) -> None:
        assert self.ads is not None
        await self.ads.delete_one({"_id": ad_id})

    async def list_ads(self) -> list[dict]:
        assert self.ads is not None
        return await self.ads.find().sort("created_at", ASCENDING).to_list(length=100)

    async def count_ads(self) -> int:
        assert self.ads is not None
        return await self.ads.count_documents({})

    async def get_random_ad(self) -> dict | None:
        assert self.ads is not None
        ads = await self.ads.aggregate([{"$sample": {"size": 1}}]).to_list(length=1)
        return ads[0] if ads else None

    async def create_support_ticket(self, user_id: int, username: str | None, title: str, text: str) -> dict:
        assert self.support_tickets is not None
        document = {
            "_id": uuid4().hex,
            "ticket_id": uuid4().hex[:10],
            "user_id": user_id,
            "username": username,
            "title": title,
            "text": text,
            "status": "open",
            "created_at": datetime.now(timezone.utc),
        }
        await self.support_tickets.insert_one(document)
        return document

    async def close_support_ticket(self, ticket_id: str, status: str) -> None:
        assert self.support_tickets is not None
        await self.support_tickets.update_one(
            {"ticket_id": ticket_id},
            {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
        )

    async def create_broadcast(self, payload: dict) -> dict:
        assert self.broadcasts is not None
        document = {
            "_id": uuid4().hex,
            "created_at": datetime.now(timezone.utc),
            "status": "running",
            **payload,
        }
        await self.broadcasts.insert_one(document)
        return document

    async def finish_broadcast(self, broadcast_id: str, stats: dict[str, int]) -> None:
        assert self.broadcasts is not None
        await self.broadcasts.update_one(
            {"_id": broadcast_id},
            {
                "$set": {
                    "status": "completed",
                    "finished_at": datetime.now(timezone.utc),
                    "stats": stats,
                }
            },
        )

    async def latest_broadcasts(self, limit: int = 5) -> list[dict]:
        assert self.broadcasts is not None
        return await self.broadcasts.find().sort("created_at", -1).to_list(length=limit)

    @staticmethod
    def build_vpn_username(user_id: int) -> str:
        return f"tg_{user_id}"


db = MongoStorage()
