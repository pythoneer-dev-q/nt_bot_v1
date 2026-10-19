import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from core.app_core import PasarGuardManager
from core.middlewares import MaintenanceMiddleware
from database.db import MongoStorage
from utils import kbs


class FakeMongoCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, filter_doc=None, *args, **kwargs):
        if not filter_doc:
            return self.docs[0] if self.docs else None
        for doc in self.docs:
            match = True
            for k, v in filter_doc.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                return doc.copy()
        return None

    async def update_one(self, filter_doc, update_doc, upsert=False, *args, **kwargs):
        existing = await self.find_one(filter_doc)
        if not existing and upsert:
            new_doc = {}
            if "$setOnInsert" in update_doc:
                new_doc.update(update_doc["$setOnInsert"])
            if "$set" in update_doc:
                new_doc.update(update_doc["$set"])
            for k, v in filter_doc.items():
                new_doc[k] = v
            self.docs.append(new_doc)
            return MagicMock(matched_count=0, modified_count=1, upserted_id="1")
        elif existing:
            if "$set" in update_doc:
                for doc in self.docs:
                    match = all(doc.get(k) == v for k, v in filter_doc.items())
                    if match:
                        doc.update(update_doc["$set"])
            return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)

    async def delete_many(self, filter_doc, *args, **kwargs):
        before = len(self.docs)
        if not filter_doc:
            self.docs = []
            return MagicMock(deleted_count=before)
        if "user_id" in filter_doc and "$nin" in filter_doc["user_id"]:
            keep_ids = set(filter_doc["user_id"]["$nin"])
            self.docs = [d for d in self.docs if d.get("user_id") in keep_ids]
            return MagicMock(deleted_count=before - len(self.docs))
        return MagicMock(deleted_count=0)

    async def count_documents(self, filter_doc=None, *args, **kwargs):
        return len(self.docs)


class TestMaintenanceAndMassDelete(unittest.TestCase):
    def setUp(self):
        self.db = MongoStorage()
        self.db.app_config = FakeMongoCollection()
        self.db.users = FakeMongoCollection()
        self.db.admins = FakeMongoCollection()

    def test_maintenance_mode_toggle(self):
        async def run():
            # Initial state
            state0 = await self.db.is_maintenance_mode()
            self.assertFalse(state0)

            # Turn ON
            await self.db.set_maintenance_mode(True)
            state1 = await self.db.is_maintenance_mode()
            self.assertTrue(state1)

            # Turn OFF
            await self.db.set_maintenance_mode(False)
            state2 = await self.db.is_maintenance_mode()
            self.assertFalse(state2)

        asyncio.run(run())

    def test_maintenance_middleware_drops_non_admin(self):
        async def run():
            middleware = MaintenanceMiddleware()
            handler = AsyncMock(return_value="OK")

            # Setup user
            non_admin_user = User(id=12345, is_bot=False, first_name="NormalUser")
            chat = Chat(id=12345, type="private")
            msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=non_admin_user, text="/start")
            event = Update(update_id=1, message=msg)

            with patch("core.middlewares.db", self.db):
                # When maintenance mode is OFF
                await self.db.set_maintenance_mode(False)
                res = await middleware(handler, event, {})
                self.assertEqual(res, "OK")
                self.assertEqual(handler.call_count, 1)

                # When maintenance mode is ON and user is NOT admin
                await self.db.set_maintenance_mode(True)
                handler.reset_mock()
                res = await middleware(handler, event, {})
                self.assertIsNone(res)
                handler.assert_not_called()

                # When user IS admin
                self.db.is_admin = AsyncMock(return_value=True)
                handler.reset_mock()
                res = await middleware(handler, event, {})
                self.assertEqual(res, "OK")
                handler.assert_called_once()

        asyncio.run(run())

    def test_delete_all_users_in_db(self):
        async def run():
            # Seed users
            self.db.users.docs = [
                {"user_id": 111, "username": "user1"},
                {"user_id": 222, "username": "user2"},
                {"user_id": 999, "username": "admin1"},
            ]
            self.db.list_support_recipient_ids = AsyncMock(return_value=[999])

            # Delete keeping admins
            deleted = await self.db.delete_all_users(keep_admins=True)
            self.assertEqual(deleted, 2)
            self.assertEqual(len(self.db.users.docs), 1)
            self.assertEqual(self.db.users.docs[0]["user_id"], 999)

        asyncio.run(run())

    def test_delete_all_panel_users(self):
        async def run():
            manager = PasarGuardManager()

            # Mock get_all_users: returns 2 users on 1st call, 0 on 2nd call
            batch1 = [{"username": "tg_111"}, {"username": "tg_222"}]
            batch2 = []
            manager.get_all_users = AsyncMock(side_effect=[batch1, batch2])
            manager.delete_user = AsyncMock(return_value=True)

            deleted_count = await manager.delete_all_panel_users()
            self.assertEqual(deleted_count, 2)
            self.assertEqual(manager.delete_user.call_count, 2)
            manager.delete_user.assert_any_call("tg_111")
            manager.delete_user.assert_any_call("tg_222")

        asyncio.run(run())

    def test_keyboards_maintenance_and_delete_all(self):
        async def run():
            # Admin panel keyboard with maintenance OFF
            markup_off = await kbs.admin_panel(maintenance_mode=False)
            texts_off = [btn.text for row in markup_off.inline_keyboard for btn in row]
            self.assertTrue(any("Техработы: [⚪️ ВЫКЛ]" in t for t in texts_off))
            cbs_off = [btn.callback_data for row in markup_off.inline_keyboard for btn in row]
            self.assertIn("admin:toggle_maintenance", cbs_off)

            # Admin panel keyboard with maintenance ON
            markup_on = await kbs.admin_panel(maintenance_mode=True)
            texts_on = [btn.text for row in markup_on.inline_keyboard for btn in row]
            self.assertTrue(any("Техработы: [🔴 ВКЛ]" in t for t in texts_on))

            # Admin rescue panel contains delete all users button
            rescue_markup = await kbs.admin_rescue_panel()
            rescue_cbs = [btn.callback_data for row in rescue_markup.inline_keyboard for btn in row]
            self.assertIn("admin:delete_all_users_prompt", rescue_cbs)

            # Confirmation keyboard
            confirm_markup = await kbs.admin_delete_all_users_confirm_keyboard()
            confirm_cbs = [btn.callback_data for row in confirm_markup.inline_keyboard for btn in row]
            self.assertIn("admin:delete_all_users_confirm", confirm_cbs)
            self.assertIn("admin:rescue", confirm_cbs)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
