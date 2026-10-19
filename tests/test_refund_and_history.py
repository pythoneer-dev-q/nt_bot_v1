from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from database.db import db
from utils import kbs


class TestRefundAndHistory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.connect()
        await db.ensure_indexes()
        self.user_ids = [999888111, 999888222, 999888333]
        await db.payments.delete_many({"user_id": {"$in": self.user_ids}})

    async def asyncTearDown(self):
        await db.payments.delete_many({"user_id": {"$in": self.user_ids}})
        await db.close()

    async def test_user_payments_history(self):
        user_id = 999888111
        uid_suffix = uuid.uuid4().hex[:6]
        id1 = f"test_topup_1_{uid_suffix}"
        id2 = f"test_topup_2_{uid_suffix}"
        id3 = f"test_tariff_3_{uid_suffix}"

        # Create orders
        o1 = await db.create_payment_order(
            user_id=user_id,
            order_id=id1,
            amount=100.0,
            days=0,
            months=0,
            pay_url="https://pay.example/1",
            payment_id=f"pay_1_{uid_suffix}",
            status="paid",
            order_type="topup",
        )
        o2 = await db.create_payment_order(
            user_id=user_id,
            order_id=id2,
            amount=200.0,
            days=0,
            months=0,
            pay_url="https://pay.example/2",
            payment_id=f"pay_2_{uid_suffix}",
            status="created",
            order_type="topup",
        )
        o3 = await db.create_payment_order(
            user_id=user_id,
            order_id=id3,
            amount=300.0,
            days=30,
            months=1,
            pay_url="",
            payment_id=f"bal_3_{uid_suffix}",
            status="paid",
            order_type="balance_tariff",
        )

        history = await db.get_user_payments(user_id=user_id, limit=10)
        self.assertGreaterEqual(len(history), 3)
        order_ids = [p["order_id"] for p in history[:3]]
        self.assertIn(id3, order_ids)
        self.assertIn(id2, order_ids)
        self.assertIn(id1, order_ids)

        # Filter by order_types
        topups_only = await db.get_user_payments(user_id=user_id, order_types=["topup"])
        for p in topups_only:
            self.assertEqual(p["order_type"], "topup")

    async def test_refund_request_and_resolution_flow(self):
        user_id = 999888222
        uid_suffix = uuid.uuid4().hex[:6]
        order_id = f"test_refund_{uid_suffix}"
        await db.create_payment_order(
            user_id=user_id,
            order_id=order_id,
            amount=500.0,
            days=0,
            months=0,
            pay_url="https://pay.example/r1",
            payment_id=f"pay_{uid_suffix}",
            status="paid",
            order_type="topup",
        )

        # Try refund with wrong user -> fails
        wrong_user = await db.request_payment_refund(order_id=order_id, user_id=12345, reason="Wrong user")
        self.assertIsNone(wrong_user)

        # Valid request
        requested = await db.request_payment_refund(order_id=order_id, user_id=user_id, reason="Случайная оплата")
        self.assertIsNotNone(requested)
        self.assertEqual(requested["status"], "refund_requested")
        self.assertEqual(requested["refund_reason"], "Случайная оплата")

        # Second request -> fails because status is already refund_requested
        second_req = await db.request_payment_refund(order_id=order_id, user_id=user_id, reason="Double request")
        self.assertIsNone(second_req)

        # Verify in pending refund requests
        pending = await db.get_pending_refund_requests(limit=10)
        pending_ids = [p["order_id"] for p in pending]
        self.assertIn(order_id, pending_ids)

        count = await db.count_pending_refund_requests()
        self.assertGreaterEqual(count, 1)

        # Admin approves
        admin_id = 100000001
        approved = await db.resolve_payment_refund(order_id=order_id, approve=True, admin_id=admin_id)
        self.assertIsNotNone(approved)
        self.assertEqual(approved["status"], "refunded")
        self.assertEqual(approved["refund_resolved_by"], admin_id)

        # Try to resolve again -> fails
        second_resolve = await db.resolve_payment_refund(order_id=order_id, approve=False, admin_id=admin_id)
        self.assertIsNone(second_resolve)

    async def test_admin_decline_refund(self):
        user_id = 999888333
        uid_suffix = uuid.uuid4().hex[:6]
        order_id = f"test_decline_{uid_suffix}"
        await db.create_payment_order(
            user_id=user_id,
            order_id=order_id,
            amount=150.0,
            days=0,
            months=0,
            pay_url="https://pay.example/r2",
            payment_id=f"pay_r2_{uid_suffix}",
            status="paid",
            order_type="topup",
        )

        req = await db.request_payment_refund(order_id=order_id, user_id=user_id, reason="Test decline")
        self.assertIsNotNone(req)

        # Admin declines
        admin_id = 100000002
        declined = await db.resolve_payment_refund(order_id=order_id, approve=False, admin_id=admin_id)
        self.assertIsNotNone(declined)
        self.assertEqual(declined["status"], "refund_declined")
        self.assertEqual(declined["refund_resolved_by"], admin_id)

    async def test_refund_keyboards(self):
        payments = [
            {"order_id": "ord_111111", "amount": 100.0, "status": "paid"},
            {"order_id": "ord_222222", "amount": 250.0, "status": "refund_requested"},
        ]
        history_kb = await kbs.payment_history_keyboard(payments)
        callback_datas = [btn.callback_data for row in history_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("user:order:ord_111111", callback_datas)
        self.assertIn("user:order:ord_222222", callback_datas)

        # Order details keyboard for paid status (shows cancel button)
        order_paid = {"order_id": "ord_111111", "status": "paid", "amount": 100.0}
        details_kb = await kbs.order_details_keyboard(order_paid)
        details_cbs = [btn.callback_data for row in details_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("pay:cancel_req:ord_111111", details_cbs)

        # Order details keyboard for refunded status (does NOT show cancel button)
        order_refunded = {"order_id": "ord_111111", "status": "refunded", "amount": 100.0}
        details_kb_ref = await kbs.order_details_keyboard(order_refunded)
        details_ref_cbs = [btn.callback_data for row in details_kb_ref.inline_keyboard for btn in row if btn.callback_data]
        self.assertNotIn("pay:cancel_req:ord_111111", details_ref_cbs)

        # Admin action keyboard
        admin_act_kb = await kbs.admin_refund_action_keyboard("ord_111111")
        act_cbs = [btn.callback_data for row in admin_act_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("admin_refund:approve:ord_111111", act_cbs)
        self.assertIn("admin_refund:decline:ord_111111", act_cbs)
        self.assertIn("admin:refund_requests", act_cbs)

    async def test_payment_history_pagination(self):
        user_id = 999888111
        uid_suffix = uuid.uuid4().hex[:6]
        for i in range(7):
            await db.create_payment_order(
                user_id=user_id,
                order_id=f"page_order_{i}_{uid_suffix}",
                amount=100.0 + i,
                days=0,
                months=0,
                pay_url="",
                payment_id=f"pay_p_{i}_{uid_suffix}",
                status="paid",
                order_type="topup",
            )

        total_count = await db.count_user_payments(user_id)
        self.assertGreaterEqual(total_count, 7)

        # Page 1 (limit 3, offset 0)
        page1 = await db.get_user_payments(user_id, limit=3, offset=0)
        self.assertEqual(len(page1), 3)

        # Page 2 (limit 3, offset 3)
        page2 = await db.get_user_payments(user_id, limit=3, offset=3)
        self.assertEqual(len(page2), 3)
        self.assertNotEqual(page1[0]["order_id"], page2[0]["order_id"])

        # Test keyboard pagination controls: page 1 of 3
        kb_page1 = await kbs.payment_history_keyboard(page1, page=1, total_pages=3)
        cbs_p1 = [btn.callback_data for row in kb_page1.inline_keyboard for btn in row if btn.callback_data]
        texts_p1 = [btn.text for row in kb_page1.inline_keyboard for btn in row]
        self.assertIn("user:history:2", cbs_p1)
        self.assertIn("📄 1/3", texts_p1)
        self.assertIn("user:profile", cbs_p1)

        # Test keyboard pagination controls: page 2 of 3
        kb_page2 = await kbs.payment_history_keyboard(page2, page=2, total_pages=3)
        cbs_p2 = [btn.callback_data for row in kb_page2.inline_keyboard for btn in row if btn.callback_data]
        texts_p2 = [btn.text for row in kb_page2.inline_keyboard for btn in row]
        self.assertIn("user:history:1", cbs_p2)
        self.assertIn("user:history:3", cbs_p2)
        self.assertIn("📄 2/3", texts_p2)

        # Test keyboard pagination controls: page 3 of 3
        kb_page3 = await kbs.payment_history_keyboard(page2, page=3, total_pages=3)
        cbs_p3 = [btn.callback_data for row in kb_page3.inline_keyboard for btn in row if btn.callback_data]
        texts_p3 = [btn.text for row in kb_page3.inline_keyboard for btn in row]
        self.assertIn("user:history:2", cbs_p3)
        self.assertNotIn("user:history:4", cbs_p3)
        self.assertIn("📄 3/3", texts_p3)

    async def test_support_keyboards(self):
        # 1. support_topics_keyboard
        topics_kb = await kbs.support_topics_keyboard()
        topics_cbs = [btn.callback_data for row in topics_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("support:topic:payment", topics_cbs)
        self.assertIn("support:topic:connect", topics_cbs)
        self.assertIn("support:topic:devices", topics_cbs)
        self.assertIn("support:topic:premium", topics_cbs)
        self.assertIn("support:topic:general", topics_cbs)
        self.assertIn("support:topic:custom", topics_cbs)
        self.assertIn("support:cancel", topics_cbs)

        # 2. support_cancel_keyboard
        cancel_kb = await kbs.support_cancel_keyboard()
        cancel_cbs = [btn.callback_data for row in cancel_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("support:cancel", cancel_cbs)

        # 3. support_confirm_keyboard
        confirm_kb = await kbs.support_confirm_keyboard()
        confirm_cbs = [btn.callback_data for row in confirm_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("support:send", confirm_cbs)
        self.assertIn("support:cancel", confirm_cbs)
