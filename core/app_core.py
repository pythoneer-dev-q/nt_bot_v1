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

    async def _request_with_fallback(self, method: str, endpoints: list[str], **kwargs) -> Any:
        last_error: MarzbanError | None = None
        for endpoint in endpoints:
            try:
                return await self._request(method, endpoint, **kwargs)
            except MarzbanError as exc:
                last_error = exc
                if exc.status_code not in {404, 405}:
                    raise
        if last_error:
            raise last_error
        raise MarzbanError(500, "No API endpoints configured")

    @staticmethod
    def _normalize_expire(value: int | str | None) -> int:
        if value is None:
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            if value.isdigit():
                return int(value)
            normalized = value.replace("Z", "+00:00")
            try:
                return int(datetime.fromisoformat(normalized).timestamp())
            except ValueError:
                return 0
        return 0

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
            "proxy_settings": proxies or {"vless": {}},
            "expire": expire_ts,
            "data_limit": data_limit,
            "data_limit_reset_strategy": reset_strategy.value,
            "note": note,
            "status": status.value,
            "group_ids": settings.default_groups
        }
        if inbounds:
            payload["inbounds"] = inbounds
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
        if "proxies" in kwargs:
            kwargs["proxy_settings"] = kwargs.pop("proxies")
        return await self._request("PUT", f"/api/user/{username}", json=kwargs)

    async def delete_user(self, username: str) -> dict:
        return await self._request("DELETE", f"/api/user/{username}")

    async def reset_usage(self, username: str) -> dict:
        return await self._request("POST", f"/api/user/{username}/reset")

    async def revoke_subscription(self, username: str) -> dict:
        return await self._request("POST", f"/api/user/{username}/revoke_sub")

    async def activate_next_plan(self, username: str) -> dict:
        return await self._request("POST", f"/api/user/{username}/active_next")

    async def set_user_disabled(self, username: str, disabled: bool) -> dict:
        return await self._request("PUT", f"/api/user/{username}/disabled", json={"disabled": disabled})

    async def get_user_hwids(self, user_id: int) -> list[dict]:
        data = await self._request("GET", f"/api/user/{user_id}/hwids")
        if isinstance(data, list):
            return data
        return data.get("hwids", [])

    async def delete_user_hwid(self, user_id: int, hwid: str) -> dict:
        return await self._request("DELETE", f"/api/user/{user_id}/hwids/{hwid}")

    async def reset_user_hwids(self, user_id: int) -> dict:
        return await self._request("POST", f"/api/user/{user_id}/hwids/reset")

    async def get_user_subscription_updates(self, username: str, offset: int = 0, limit: int = 10) -> list[dict]:
        data = await self._request(
            "GET",
            f"/api/user/{username}/sub_update",
            params={"offset": offset, "limit": limit},
        )
        if isinstance(data, list):
            return data
        return data.get("updates", [])

    async def get_user_usage(self, username: str, period: str = "day") -> dict:
        return await self._request("GET", f"/api/user/{username}/usage", params={"period": period})

    async def get_all_users(self, offset: int = 0, limit: int = 100, search: str | None = None) -> list[dict]:
        params = {"offset": offset, "limit": limit}
        if search:
            params["search"] = search
        data = await self._request("GET", "/api/users", params=params)
        return data.get("users", [])

    async def get_templates(self) -> list[dict]:
        data = await self._request_with_fallback("GET", ["/api/user_templates", "/api/user_template"])
        if isinstance(data, list):
            return data
        return data.get("templates", [])

    async def create_user_from_template(self, username: str, template_id: int) -> dict:
        payload = {"username": username, "user_template_id": template_id}
        return await self._request("POST", "/api/user/from_template", json=payload)

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
        current_expire = self._normalize_expire(user.get("expire")) or int(datetime.now(timezone.utc).timestamp())
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
            payload["proxy_settings"] = settings.expired_subscription_proxies
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
