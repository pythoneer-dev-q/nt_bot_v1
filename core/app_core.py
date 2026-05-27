from __future__ import annotations

import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from config import settings


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LIMITED = "limited"
    EXPIRED = "expired"
    ON_HOLD = "on_hold"


class ResetStrategy(str, Enum):
    NO_RESET = "no_reset"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class MarzbanError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Marzban Error [{status_code}]: {detail}")
        self.status_code = status_code
        self.detail = detail


class MarzbanManager:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.marzban_url).rstrip("/")
        self.username = settings.marzban_username
        self.password = settings.marzban_password
        self.token: str | None = None
        self._token_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_token(self) -> str:
        if self.token:
            return self.token

        async with self._token_lock:
            if self.token:
                return self.token

            response = await self.client.post(
                "/api/admin/token",
                data={
                    "username": self.username,
                    "password": self.password,
                    "grant_type": "password",
                },
            )
            if response.status_code >= 400:
                raise MarzbanError(response.status_code, response.text)
            self.token = response.json()["access_token"]
            return self.token

    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        token = await self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        response = await self.client.request(method, endpoint, headers=headers, **kwargs)
        if response.status_code == 401:
            self.token = None
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
            response = await self.client.request(method, endpoint, headers=headers, **kwargs)

        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                pass
            raise MarzbanError(response.status_code, str(detail))

        if not response.content:
            return {}
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text

    async def create_user(
        self,
        username: str,
        data_limit: int = 0,
        expire_days: int | None = None,
        expire: int | None = None,
        proxies: dict[str, dict] | None = None,
        inbounds: dict[str, Any] | None = None,
        note: str = "",
        reset_strategy: ResetStrategy = ResetStrategy.NO_RESET,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> dict:
        expire_ts = 0
        if expire is not None:
            expire_ts = expire
        elif expire_days:
            expire_ts = int((datetime.now(timezone.utc) + timedelta(days=expire_days)).timestamp())

        payload = {
            "username": username,
            "proxies": proxies or {"vless": {}},
            "inbounds": inbounds or {},
            "expire": expire_ts,
            "data_limit": data_limit,
            "data_limit_reset_strategy": reset_strategy.value,
            "note": note,
            "status": status.value,
        }
        return await self._request("POST", "/api/user", json=payload)

    async def get_user(self, username: str) -> dict:
        return await self._request("GET", f"/api/user/{username}")

    async def get_user_or_none(self, username: str) -> dict | None:
        try:
            return await self.get_user(username)
        except MarzbanError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def modify_user(self, username: str, **kwargs) -> dict:
        return await self._request("PUT", f"/api/user/{username}", json=kwargs)

    async def delete_user(self, username: str) -> dict:
        return await self._request("DELETE", f"/api/user/{username}")

    async def reset_usage(self, username: str) -> dict:
        return await self._request("POST", f"/api/user/{username}/reset")

    async def get_all_users(self, offset: int = 0, limit: int = 100, search: str | None = None) -> list[dict]:
        params = {"offset": offset, "limit": limit}
        if search:
            params["search"] = search
        data = await self._request("GET", "/api/users", params=params)
        return data.get("users", [])

    async def get_templates(self) -> list[dict]:
        return await self._request("GET", "/api/user_template")

    async def create_user_from_template(self, username: str, template_id: int) -> dict:
        template = await self._request("GET", f"/api/user_template/{template_id}")
        expire_ts = 0
        if template.get("expire_duration"):
            expire_ts = int(datetime.now(timezone.utc).timestamp() + template["expire_duration"])

        payload = {
            "username": username,
            "data_limit": template.get("data_limit", 0),
            "expire": expire_ts,
            "inbounds": template.get("inbounds", {}),
            "proxies": template.get("proxies") or {"vless": {}},
        }
        return await self._request("POST", "/api/user", json=payload)

    async def get_system_stats(self) -> dict:
        return await self._request("GET", "/api/system")

    async def get_nodes(self) -> list[dict]:
        return await self._request("GET", "/api/nodes")

    async def get_inbounds(self) -> dict:
        return await self._request("GET", "/api/inbounds")

    async def get_subscription_link(self, username: str) -> str:
        user = await self.get_user(username)
        return user.get("subscription_url", "")

    async def ensure_subscription_user(
        self,
        username: str,
        data_limit_gb: int,
        expire_days: int,
        note: str = "",
    ) -> dict:
        user = await self.get_user_or_none(username)
        if user:
            return user

        data_limit = data_limit_gb * (1024**3)
        await self.create_user(
            username=username,
            data_limit=data_limit,
            expire_days=expire_days,
            note=note,
        )
        return await self.get_user(username)

    async def extend_user_subscription(self, username: str, days: int, reset_usage: bool = True) -> dict:
        user = await self.get_user(username)
        current_expire = user.get("expire") or int(datetime.now(timezone.utc).timestamp())
        now_ts = int(datetime.now(timezone.utc).timestamp())
        base_ts = max(current_expire, now_ts)
        new_expire = base_ts + (days * 86400)
        updated_user = await self.modify_user(username, expire=new_expire, status=UserStatus.ACTIVE.value)
        if reset_usage:
            await self.reset_usage(username)
            return await self.get_user(username)
        return updated_user

    async def set_user_status(self, username: str, status: UserStatus) -> dict:
        return await self.modify_user(username, status=status.value)

    async def apply_expired_subscription_config(self, username: str) -> dict:
        payload: dict[str, Any] = {
            "status": UserStatus.EXPIRED.value,
            "note": settings.expired_subscription_note,
        }
        if settings.expired_subscription_proxies:
            payload["proxies"] = settings.expired_subscription_proxies
        if settings.expired_subscription_inbounds:
            payload["inbounds"] = settings.expired_subscription_inbounds
        return await self.modify_user(username, **payload)

    @staticmethod
    def format_bytes(size_bytes: int) -> str:
        if size_bytes == 0:
            return "0 B"
        size = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PB"


manager = MarzbanManager()
