from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
from database.db import db
from core.app_core import manager
from utils import kbs
from config import settings


class TestDeviceLimitsAndRevocation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.connect()
        await db.ensure_indexes()
        self.user_id = 999111222
        await db.users.delete_one({"user_id": self.user_id})

    async def asyncTearDown(self):
        await db.users.delete_one({"user_id": self.user_id})
        await db.close()

    async def test_user_device_limits_default_and_custom(self):
        # Ensure user exists as free
        await db.ensure_user(self.user_id, "test_dev_user", "Test Device User")
        await db.set_user_tariff(self.user_id, "free", None)

        free_limit = await db.get_user_device_limit(self.user_id)
        self.assertEqual(free_limit, settings.free_device_limit)

        # Switch to premium
        await db.set_user_tariff(self.user_id, "premium", None)
        prem_limit = await db.get_user_device_limit(self.user_id)
        self.assertEqual(prem_limit, settings.premium_device_limit)

        # Set custom device limit (e.g. 5)
        await db.set_user_custom_device_limit(self.user_id, 5)
        custom_limit = await db.get_user_device_limit(self.user_id)
        self.assertEqual(custom_limit, 5)

        # Reset custom device limit to None
        await db.set_user_custom_device_limit(self.user_id, None)
        reverted_limit = await db.get_user_device_limit(self.user_id)
        self.assertEqual(reverted_limit, settings.premium_device_limit)

    async def test_should_warn_device_limit(self):
        await db.ensure_user(self.user_id, "test_warn_user", "Test Warn User")

        # Current count below limit -> no warning
        warn = await db.should_warn_device_limit(self.user_id, current_count=1, limit=3)
        self.assertFalse(warn)

        # Current count reaches limit -> warn
        warn_limit = await db.should_warn_device_limit(self.user_id, current_count=3, limit=3)
        self.assertTrue(warn_limit)

        # Mark as warned
        await db.mark_device_limit_warned(self.user_id, current_count=3)

        # Check again immediately with same count -> do not re-warn
        re_warn = await db.should_warn_device_limit(self.user_id, current_count=3, limit=3)
        self.assertFalse(re_warn)

        # Current count increased -> warn again
        warn_higher = await db.should_warn_device_limit(self.user_id, current_count=4, limit=3)
        self.assertTrue(warn_higher)

    async def test_devices_keyboards(self):
        # Empty devices
        empty_kb = await kbs.devices_keyboard([], device_limit=2)
        cbs_empty = [b.callback_data for row in empty_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:sub_settings", cbs_empty)
        self.assertIn("user:menu", cbs_empty)
        self.assertNotIn("user:profile", cbs_empty)
        self.assertNotIn("user:dev_reset_all", cbs_empty)

        # Multiple devices - clicking a device opens user:dev:{short_hwid}
        hwids = [
            {"hwid": "hwid_abcdef_123456", "device_model": "iPhone 15", "device_os": "iOS"},
            {"hwid": "hwid_uvwxyz_789012", "device_model": "Pixel 8", "device_os": "Android"},
        ]
        multi_kb = await kbs.devices_keyboard(hwids, device_limit=2)
        cbs_multi = [b.callback_data for row in multi_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:dev:hwid_abcdef_123456", cbs_multi)
        self.assertIn("user:dev:hwid_uvwxyz_789012", cbs_multi)
        self.assertIn("user:dev_reset_all", cbs_multi)
        self.assertIn("user:sub_settings", cbs_multi)
        self.assertNotIn("user:profile", cbs_multi)

        # Device details card keyboard
        dev_card_kb = await kbs.device_details_keyboard("hwid_abcdef_123456")
        card_cbs = [b.callback_data for row in dev_card_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:dev_del:hwid_abcdef_123456", card_cbs)
        self.assertIn("user:devices", card_cbs)
        self.assertIn("user:sub_settings", card_cbs)
        self.assertNotIn("user:profile", card_cbs)

        # Delete confirmation
        del_conf_kb = await kbs.device_delete_confirm_keyboard("hwid_abcdef_123456")
        del_cbs = [b.callback_data for row in del_conf_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:dev_del_conf:hwid_abcdef_123456", del_cbs)
        self.assertIn("user:dev:hwid_abcdef_123456", del_cbs)
        self.assertNotIn("user:profile", del_cbs)

        # Reset all confirmation
        reset_all_kb = await kbs.device_reset_all_confirm_keyboard()
        reset_cbs = [b.callback_data for row in reset_all_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:dev_reset_all_conf", reset_cbs)
        self.assertIn("user:devices", reset_cbs)

        # Reset sub confirmation
        reset_sub_kb = await kbs.reset_sub_confirm_keyboard()
        sub_cbs = [b.callback_data for row in reset_sub_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:reset_sub_conf", sub_cbs)
        self.assertIn("user:sub_settings", sub_cbs)
        self.assertNotIn("user:profile", sub_cbs)

        # Subscription settings keyboard
        settings_free_kb = await kbs.sub_settings_keyboard(is_premium=False, auto_renew=False, devices_count=1, device_limit=1)
        free_set_cbs = [b.callback_data for row in settings_free_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:devices", free_set_cbs)
        self.assertIn("user:reset_sub", free_set_cbs)
        self.assertIn("user:connect", free_set_cbs)
        self.assertNotIn("user:toggle_autorenew", free_set_cbs)

        settings_prem_kb = await kbs.sub_settings_keyboard(is_premium=True, auto_renew=True, devices_count=2, device_limit=3)
        prem_set_cbs = [b.callback_data for row in settings_prem_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:devices", prem_set_cbs)
        self.assertIn("user:reset_sub", prem_set_cbs)
        self.assertIn("user:toggle_autorenew", prem_set_cbs)
        self.assertIn("user:connect", prem_set_cbs)

        # Profile keyboard should not have devices or reset sub
        prof_kb = await kbs.profile_keyboard(can_overdraft=True)
        prof_cbs = [b.callback_data for row in prof_kb.inline_keyboard for b in row if b.callback_data]
        self.assertNotIn("user:devices", prof_cbs)
        self.assertNotIn("user:reset_sub", prof_cbs)
        self.assertIn("user:topup", prof_cbs)
        self.assertIn("user:history", prof_cbs)
        self.assertIn("user:overdraft", prof_cbs)

        # Admin device keyboards
        admin_limits_kb = await kbs.admin_device_limits_keyboard()
        adm_cbs = [b.callback_data for row in admin_limits_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("admin:set_free_device_limit", adm_cbs)
        self.assertIn("admin:set_premium_device_limit", adm_cbs)

        user_act_kb = await kbs.admin_user_actions(self.user_id, is_premium=False)
        act_cbs = [b.callback_data for row in user_act_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn(f"admin_user:set_limit:{self.user_id}", act_cbs)
        self.assertIn(f"admin_user:reset_hwids:{self.user_id}", act_cbs)
        self.assertIn(f"admin_user:revoke_sub:{self.user_id}", act_cbs)

    async def test_revoke_subscription_call(self):
        mock_response = {
            "username": "user_test_999",
            "subscription_url": "https://vpn.example/sub/new_token_12345",
        }
        with patch.object(manager, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response
            res = await manager.revoke_user_subscription("user_test_999")
            mock_req.assert_called_once_with("POST", "/api/user/user_test_999/revoke_sub")
            self.assertEqual(res["subscription_url"], "https://vpn.example/sub/new_token_12345")
