from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
import httpx
from aiogram import Bot
from config import settings
from core.app_core import manager
from database.db import db

logger = logging.getLogger(__name__)


class RollyPaymentError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"RollyPay Error [{status_code}]: {detail}")
        self.status_code = status_code
        self.detail = detail


class RollyPaymentService:
    def __init__(self) -> None:
        self.base_url = settings.rolly_api_url.rstrip("/")
        self.api_key = settings.rolly_api_key
        self.terminal_id = settings.rolly_terminal_id
        self.signing_secret = settings.rolly_signing_secret
        self.client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def create_payment(
        self,
        user_id: int,
        amount: float,
        days: int,
        months: int,
        description: str | None = None,
    ) -> dict[str, Any]:
        order_id = f"ord_{uuid4().hex[:12]}"
        amount_str = f"{amount:.2f}"
        desc = description or f"Оплата VPN подписки на {months} мес."

        payload: dict[str, Any] = {
            "amount": amount_str,
            "payment_currency": "RUB",
            "order_id": order_id,
            "description": desc,
            "customer_id": f"tg_{user_id}",
            "metadata": {
                "user_id": user_id,
                "order_id": order_id,
                "days": days,
                "months": months,
            },
        }
        if self.terminal_id:
            payload["terminal_id"] = self.terminal_id

        # If API key is configured, call RollyPay API
        if self.api_key:
            try:
                response = await self.client.post(
                    f"{self.base_url}/api/v1/payments",
                    json=payload,
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    logger.error("RollyPay create payment error: %s %s", response.status_code, response.text)
                    raise RollyPaymentError(response.status_code, response.text)

                data = response.json()
                payment_id = data.get("payment_id", f"pay_{uuid4().hex[:16]}")
                pay_url = data.get("pay_url", "")

                await db.create_payment_order(
                    user_id=user_id,
                    order_id=order_id,
                    amount=amount,
                    days=days,
                    months=months,
                    pay_url=pay_url,
                    payment_id=payment_id,
                    status=data.get("status", "created"),
                )
                return {
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "pay_url": pay_url,
                    "amount": amount,
                    "status": data.get("status", "created"),
                }
            except RollyPaymentError:
                raise
            except Exception as exc:
                logger.exception("Failed to create RollyPay payment via API: %s", exc)
                raise

        # Sandbox / Dev mode fallback when ROLLY_API_KEY is not yet filled in .env
        mock_payment_id = f"pay_mock_{uuid4().hex[:12]}"
        mock_pay_url = f"https://pay.rollypay.io/pay/tok_demo_{uuid4().hex[:8]}"
        await db.create_payment_order(
            user_id=user_id,
            order_id=order_id,
            amount=amount,
            days=days,
            months=months,
            pay_url=mock_pay_url,
            payment_id=mock_payment_id,
            status="created",
        )
        return {
            "order_id": order_id,
            "payment_id": mock_payment_id,
            "pay_url": mock_pay_url,
            "amount": amount,
            "status": "created",
            "is_sandbox": True,
        }

    async def create_topup_payment(
        self,
        user_id: int,
        amount: float,
    ) -> dict[str, Any]:
        order_id = f"top_{uuid4().hex[:12]}"
        amount_str = f"{amount:.2f}"
        desc = f"Пополнение баланса на {amount:.2f} ₽"

        payload: dict[str, Any] = {
            "amount": amount_str,
            "payment_currency": "RUB",
            "order_id": order_id,
            "description": desc,
            "customer_id": f"tg_{user_id}",
            "metadata": {
                "user_id": user_id,
                "order_id": order_id,
                "order_type": "topup",
            },
        }
        if self.terminal_id:
            payload["terminal_id"] = self.terminal_id

        if self.api_key:
            try:
                response = await self.client.post(
                    f"{self.base_url}/api/v1/payments",
                    json=payload,
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    logger.error("RollyPay create topup payment error: %s %s", response.status_code, response.text)
                    raise RollyPaymentError(response.status_code, response.text)

                data = response.json()
                payment_id = data.get("payment_id", f"pay_{uuid4().hex[:16]}")
                pay_url = data.get("pay_url", "")

                await db.create_payment_order(
                    user_id=user_id,
                    order_id=order_id,
                    amount=amount,
                    pay_url=pay_url,
                    payment_id=payment_id,
                    status=data.get("status", "created"),
                    order_type="topup",
                )
                return {
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "pay_url": pay_url,
                    "amount": amount,
                    "status": data.get("status", "created"),
                    "order_type": "topup",
                }
            except RollyPaymentError:
                raise
            except Exception as exc:
                logger.exception("Failed to create RollyPay topup payment via API: %s", exc)
                raise

        mock_payment_id = f"pay_mock_{uuid4().hex[:12]}"
        mock_pay_url = f"https://pay.rollypay.io/pay/tok_demo_{uuid4().hex[:8]}"
        await db.create_payment_order(
            user_id=user_id,
            order_id=order_id,
            amount=amount,
            pay_url=mock_pay_url,
            payment_id=mock_payment_id,
            status="created",
            order_type="topup",
        )
        return {
            "order_id": order_id,
            "payment_id": mock_payment_id,
            "pay_url": mock_pay_url,
            "amount": amount,
            "status": "created",
            "is_sandbox": True,
            "order_type": "topup",
        }

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        if not self.api_key or payment_id.startswith("pay_mock_"):
            order = await db.get_payment_by_payment_id(payment_id)
            return {"payment_id": payment_id, "status": order.get("status", "created") if order else "created"}

        response = await self.client.get(
            f"{self.base_url}/api/v1/payments/{payment_id}",
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise RollyPaymentError(response.status_code, response.text)
        return response.json()

    def verify_webhook_signature(self, raw_body: bytes, timestamp: str, signature: str) -> bool:
        secret = self.signing_secret
        if not secret:
            return True
        mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
        mac.update(timestamp.encode("utf-8"))
        mac.update(b".")
        mac.update(raw_body)
        expected = mac.hexdigest()
        return hmac.compare_digest(expected.lower(), signature.lower())

    async def process_payment_success(self, order_id: str, bot: Bot | None = None) -> bool:
        order = await db.get_payment_order(order_id)
        if not order:
            logger.warning("Payment order not found: %s", order_id)
            return False

        if order.get("status") == "paid":
            return True

        user_id = order["user_id"]
        order_type = order.get("order_type", "tariff")
        amount = order.get("amount", 0.0)

        # Mark paid in DB
        await db.mark_payment_paid(order_id)

        if order_type == "topup":
            new_balance = await db.change_user_balance(user_id, amount)
            if bot:
                try:
                    await bot.send_message(
                        user_id,
                        (
                            "🎉 <b>Баланс успешно пополнен!</b>\n\n"
                            f"Зачислено: <b>+{amount:.2f} ₽</b>\n"
                            f"Текущий баланс: <b>{new_balance:.2f} ₽</b>"
                        ),
                    )
                except Exception as exc:
                    logger.warning("Could not send topup notification to %s: %s", user_id, exc)
            return True

        days = order.get("days", 30)
        now = datetime.now(timezone.utc)
        premium_expires_at = now + timedelta(days=days)

        # Update local user record
        await db.set_user_tariff(user_id, "premium", premium_expires_at)

        # Update PasarGuard user (group aska_premium + expire)
        username = db.build_vpn_username(user_id)
        try:
            await manager.upgrade_user_to_premium(username, days)
        except Exception as exc:
            logger.exception("Failed to upgrade PasarGuard user %s to premium: %s", username, exc)

        # Notify user in bot
        if bot:
            try:
                await bot.send_message(
                    user_id,
                    (
                        "🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"Тариф <b>Премиум</b> активирован на <b>{days} дней</b>.\n"
                        f"Действует до: <code>{premium_expires_at.strftime('%d.%m.%Y %H:%M UTC')}</code>\n\n"
                        "⚡️ Вы подключены к высокоскоростным премиум-серверам без рекламы!"
                    ),
                )
            except Exception as exc:
                logger.warning("Could not send payment success notification to %s: %s", user_id, exc)

        return True


class PaymentMonitorService:
    def __init__(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def start(self, bot: Bot | None = None) -> None:
        if bot:
            self.bot = bot
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="payment-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def check_pending_payments(self) -> int:
        pending = await db.get_pending_payments(older_than_minutes=120)
        checked_and_paid = 0

        for order in pending:
            payment_id = order.get("payment_id")
            if not payment_id or payment_id.startswith("pay_mock_"):
                continue

            try:
                data = await payment_service.get_payment(payment_id)
                status = data.get("status")
                if status == "paid":
                    await payment_service.process_payment_success(order["order_id"], bot=self.bot)
                    checked_and_paid += 1
                elif status in {"canceled", "expired"}:
                    await db.update_payment_status(order["order_id"], status)
            except Exception as exc:
                logger.debug("Payment check for order %s failed: %s", order.get("order_id"), exc)

        return checked_and_paid

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.check_pending_payments()
            except Exception as exc:
                logger.warning("Payment monitor cycle error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                continue


payment_service = RollyPaymentService()
payment_monitor_service = PaymentMonitorService()

