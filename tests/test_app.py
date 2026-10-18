from __future__ import annotations

import asyncio
import hashlib
import hmac
import unittest
from datetime import datetime, timedelta, timezone
from config import settings
from core.app_core import PasarGuardManager
from core.tariffs import calculate_tariffs, get_tariff_by_period
from database.db import db
from services.payments import RollyPaymentService


class TestTariffsAndDiscounts(unittest.TestCase):
    def test_discount_progression_doubling(self):
        # Base discount = 10%
        base_price = 200.0
        tariffs = calculate_tariffs(base_price=base_price, discount_percent_base=10.0)
        self.assertEqual(len(tariffs), 4)

        t1m, t3m, t6m, t12m = tariffs

        # 1 month: 0% discount
        self.assertEqual(t1m.months, 1)
        self.assertEqual(t1m.discount_percent, 0.0)
        self.assertEqual(t1m.total_price, 200)
        self.assertEqual(t1m.price_per_month, 200.0)

        # 3 months: 10% discount (D)
        self.assertEqual(t3m.months, 3)
        self.assertEqual(t3m.discount_percent, 10.0)
        self.assertEqual(t3m.total_price, 540)  # 200 * 3 * 0.9 = 540
        self.assertEqual(t3m.price_per_month, 180.0)
        self.assertEqual(t3m.savings, 60)

        # 6 months: 20% discount (2 * D, doubled!)
        self.assertEqual(t6m.months, 6)
        self.assertEqual(t6m.discount_percent, 20.0)
        self.assertEqual(t6m.total_price, 960)  # 200 * 6 * 0.8 = 960
        self.assertEqual(t6m.price_per_month, 160.0)
        self.assertEqual(t6m.savings, 240)

        # 12 months: 40% discount (4 * D, doubled again!)
        self.assertEqual(t12m.months, 12)
        self.assertEqual(t12m.discount_percent, 40.0)
        self.assertEqual(t12m.total_price, 1440)  # 200 * 12 * 0.6 = 1440
        self.assertEqual(t12m.price_per_month, 120.0)
        self.assertEqual(t12m.savings, 960)

        # Check that price per month is strictly decreasing
        self.assertGreater(t1m.price_per_month, t3m.price_per_month)
        self.assertGreater(t3m.price_per_month, t6m.price_per_month)
        self.assertGreater(t6m.price_per_month, t12m.price_per_month)

    def test_get_tariff_by_period(self):
        t = get_tariff_by_period("3m", 199.0, 10.0)
        self.assertIsNotNone(t)
        self.assertEqual(t.months, 3)
        self.assertEqual(t.period_id, "3m")

        t12 = get_tariff_by_period("12", 199.0, 10.0)
        self.assertIsNotNone(t12)
        self.assertEqual(t12.months, 12)

        self.assertIsNone(get_tariff_by_period("unknown", 199.0, 10.0))


class TestPasarGuardGroupSearch(unittest.IsolatedAsyncioTestCase):
    async def test_search_group_by_name(self):
        manager = PasarGuardManager()

        # Mock get_all_groups response
        fake_groups = [
            {"id": 42, "name": "other_group", "is_disabled": False},
            {"id": 101, "name": "aska_free", "is_disabled": False},
            {"id": 202, "name": "ASKA_PREMIUM", "is_disabled": False},
        ]

        async def fake_get_all_groups():
            return fake_groups

        manager.get_all_groups = fake_get_all_groups

        # Search by exact name
        grp_free = await manager.get_group_by_name("aska_free")
        self.assertIsNotNone(grp_free)
        self.assertEqual(grp_free["id"], 101)

        # Search case-insensitively
        grp_premium = await manager.get_group_by_name("aska_premium")
        self.assertIsNotNone(grp_premium)
        self.assertEqual(grp_premium["id"], 202)

        # Test ID lookup with caching
        id_free = await manager.get_group_id_by_name("aska_free")
        self.assertEqual(id_free, 101)
        self.assertIn("aska_free", manager._group_cache)

        # Test non-existent group
        id_unknown = await manager.get_group_id_by_name("non_existent_group")
        self.assertIsNone(id_unknown)

        await manager.close()


class TestRollyPayIntegration(unittest.TestCase):
    def test_signature_verification(self):
        service = RollyPaymentService()
        secret = "super_secret_signing_key_123"
        service.signing_secret = secret

        timestamp = "1771848000"
        raw_body = b'{"event_type":"payment.paid","payment_id":"pay_123","status":"paid","amount":"199.00"}'

        # Compute valid signature
        mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
        mac.update(timestamp.encode("utf-8"))
        mac.update(b".")
        mac.update(raw_body)
        valid_signature = mac.hexdigest()

        # Test valid
        self.assertTrue(service.verify_webhook_signature(raw_body, timestamp, valid_signature))

        # Test tampered body
        tampered_body = b'{"event_type":"payment.paid","payment_id":"pay_123","status":"paid","amount":"9999.00"}'
        self.assertFalse(service.verify_webhook_signature(tampered_body, timestamp, valid_signature))

        # Test wrong timestamp
        self.assertFalse(service.verify_webhook_signature(raw_body, "1771849000", valid_signature))

        # Test wrong signature
        self.assertFalse(service.verify_webhook_signature(raw_body, timestamp, "bad_signature"))


class TestDatabaseAndTariffLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.connect()
        await db.ensure_indexes()

    async def asyncTearDown(self):
        await db.close()

    async def test_app_config_persistence(self):
        # Read initial
        cfg = await db.get_app_config()
        self.assertIn("premium_base_price_rub", cfg)
        self.assertIn("default_discount_percent", cfg)

        # Update
        updated = await db.update_app_config(premium_base_price_rub=250.0, default_discount_percent=12.5)
        self.assertEqual(updated["premium_base_price_rub"], 250.0)
        self.assertEqual(updated["default_discount_percent"], 12.5)

        # Re-fetch
        fetched = await db.get_app_config()
        self.assertEqual(fetched["premium_base_price_rub"], 250.0)
        self.assertEqual(fetched["default_discount_percent"], 12.5)

    async def test_user_tariff_auto_expiration(self):
        test_user_id = 999999991
        await db.ensure_user(test_user_id, "test_expire_user", "Test Expiration")

        # Set user as premium with future expiration
        future_dt = datetime.now(timezone.utc) + timedelta(days=30)
        await db.set_user_tariff(test_user_id, "premium", future_dt)
        self.assertEqual(await db.get_user_tariff(test_user_id), "premium")

        # Set user as premium with past expiration
        past_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.set_user_tariff(test_user_id, "premium", past_dt)
        # get_user_tariff should detect expiration and return 'free'
        tariff = await db.get_user_tariff(test_user_id)
        self.assertEqual(tariff, "free")

    async def test_payment_order_lifecycle(self):
        from uuid import uuid4
        test_user_id = 999999992
        order_id = f"test_ord_{uuid4().hex[:8]}"
        await db.create_payment_order(
            user_id=test_user_id,
            order_id=order_id,
            amount=540.0,
            days=90,
            months=3,
            pay_url="https://pay.example.com",
            payment_id="pay_test_001",
        )

        order = await db.get_payment_order(order_id)
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "created")
        self.assertEqual(order["amount"], 540.0)
        self.assertEqual(order["days"], 90)

        # Mark paid
        paid_order = await db.mark_payment_paid(order_id)
        self.assertIsNotNone(paid_order)
        self.assertEqual(paid_order["status"], "paid")
        self.assertIsNotNone(paid_order["paid_at"])

    async def test_user_balance_and_overdraft(self):
        test_user_id = 999999993
        await db.ensure_user(test_user_id, "test_balance_user", "Test Balance")

        # Initial balance is 0
        bal = await db.get_user_balance(test_user_id)
        self.assertEqual(bal, 0.0)

        # Overdraft not allowed when balance <= 0
        can_use, reason = await db.can_use_overdraft(test_user_id)
        self.assertFalse(can_use)
        self.assertIn("положительном балансе", reason)

        # Top up balance to positive
        new_bal = await db.change_user_balance(test_user_id, 15.0)
        self.assertEqual(new_bal, 15.0)

        # Now overdraft IS allowed
        can_use, _ = await db.can_use_overdraft(test_user_id)
        self.assertTrue(can_use)

        # Apply overdraft with special price 29.0 (should drive balance into negative)
        updated = await db.apply_overdraft(test_user_id, price=29.0, days=3)
        self.assertIsNotNone(updated)
        bal_after = await db.get_user_balance(test_user_id)
        self.assertEqual(bal_after, -14.0)  # 15.0 - 29.0 = -14.0 (negative balance!)
        self.assertEqual(await db.get_user_tariff(test_user_id), "premium")

        # Second overdraft attempt must be rejected (only once allowed!)
        can_use_again, reason_again = await db.can_use_overdraft(test_user_id)
        self.assertFalse(can_use_again)

        # Clean up
        await db.delete_user(test_user_id)

    async def test_auto_renew_toggle(self):
        test_user_id = 999999994
        await db.ensure_user(test_user_id, "test_ar_user", "Test AutoRenew")

        self.assertTrue(await db.get_user_auto_renew(test_user_id))
        await db.set_user_auto_renew(test_user_id, False)
        self.assertFalse(await db.get_user_auto_renew(test_user_id))
        await db.delete_user(test_user_id)

    async def test_recreate_user(self):
        test_user_id = 999999995
        await db.ensure_user(test_user_id, "old_name", "Old Name")
        await db.set_user_balance(test_user_id, 50.0)

        # Recreate resets the user
        fresh_user = await db.recreate_user(test_user_id, "new_name", "New Name")
        self.assertEqual(fresh_user["username"], "new_name")
        self.assertEqual(fresh_user["balance"], 0.0)
        await db.delete_user(test_user_id)


class TestSafeTimestamp(unittest.TestCase):
    def test_safe_int_ts_various_inputs(self):
        from core.app_core import safe_int_ts

        # PasarGuard string integer (the exact case that failed before)
        self.assertEqual(safe_int_ts("1776781804"), 1776781804)
        self.assertEqual(safe_int_ts(" 1776781804 "), 1776781804)

        # Standard int and float
        self.assertEqual(safe_int_ts(1776781804), 1776781804)
        self.assertEqual(safe_int_ts(1776781804.5), 1776781804)
        self.assertEqual(safe_int_ts("1776781804.0"), 1776781804)

        # Null / None / empty
        self.assertEqual(safe_int_ts(None), 0)
        self.assertEqual(safe_int_ts(""), 0)
        self.assertEqual(safe_int_ts("null"), 0)
        self.assertEqual(safe_int_ts("None"), 0)

        # ISO format
        self.assertGreater(safe_int_ts("2026-10-18T20:00:00Z"), 0)


if __name__ == "__main__":
    unittest.main()
