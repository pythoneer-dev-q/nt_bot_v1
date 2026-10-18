from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
import httpx
from config import settings

logger = logging.getLogger(__name__)


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
        super().__init__(f"PasarGuard Error [{status_code}]: {detail}")
        self.status_code = status_code
        self.detail = detail


def safe_int_ts(val: Any) -> int:
    """Безопасно преобразует timestamp из PasarGuard (число, строка, ISO или None) в целочисленный Unix-timestamp."""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val = val.strip()
        if not val or val.lower() in {"null", "none"}:
            return 0
        if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
            return int(val)
        try:
            val_clean = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(val_clean)
            return int(dt.timestamp())
        except Exception:
            try:
                return int(float(val))
            except Exception:
                return 0
    return 0


def normalize_vpn_user(user: Any) -> Any:
    if isinstance(user, dict):
        if "expire" in user:
            user["expire"] = safe_int_ts(user["expire"])
    return user


class PasarGuardManager:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.marzban_url).rstrip("/")
        self.username = settings.marzban_username
        self.password = settings.marzban_password
        self.token: str | None = settings.marzban_token or None
        self._token_lock = asyncio.Lock()
        self._group_cache: dict[str, int] = {}
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            follow_redirects=True,
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

    # ---------------- Group Resolution by Name ----------------
    def clear_group_cache(self) -> None:
        self._group_cache.clear()

    async def get_all_groups(self) -> list[dict]:
        try:
            data = await self._request("GET", "/api/groups")
            if isinstance(data, dict):
                return data.get("groups", [])
            if isinstance(data, list):
                return data
            return []
        except Exception as exc:
            logger.warning("Failed to fetch groups from PasarGuard: %s", exc)
            return []

    async def get_group_by_name(self, group_name: str) -> dict | None:
        groups = await self.get_all_groups()
        target = group_name.strip().lower()
        for group in groups:
            if group.get("name", "").strip().lower() == target:
                return group
        return None

    async def get_group_id_by_name(self, group_name: str) -> int | None:
        target = group_name.strip().lower()
        if target in self._group_cache:
            return self._group_cache[target]

        group = await self.get_group_by_name(group_name)
        if group and "id" in group:
            group_id = int(group["id"])
            self._group_cache[target] = group_id
            return group_id
        return None

    # ---------------- User Management ----------------
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
        group_ids: list[int] | None = None,
        hwid_limit: int | None = None,
    ) -> dict:
        expire_ts = 0
        if expire is not None:
            expire_ts = expire
        elif expire_days:
            expire_ts = int((datetime.now(timezone.utc) + timedelta(days=expire_days)).timestamp())

        payload: dict[str, Any] = {
            "username": username,
            "proxies": proxies or {"vless": {}},
            "inbounds": inbounds or {},
            "expire": expire_ts,
            "data_limit": data_limit,
            "data_limit_reset_strategy": reset_strategy.value,
            "note": note,
            "status": status.value,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
        if hwid_limit is not None:
            payload["hwid_limit"] = hwid_limit

        res = await self._request("POST", "/api/user", json=payload)
        return normalize_vpn_user(res) if isinstance(res, dict) else res

    async def get_user(self, username: str) -> dict:
        user = await self._request("GET", f"/api/user/{username}")
        return normalize_vpn_user(user) if isinstance(user, dict) else user

    async def get_user_or_none(self, username: str) -> dict | None:
        try:
            return await self.get_user(username)
        except MarzbanError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def modify_user(self, username: str, **kwargs) -> dict:
        res = await self._request("PUT", f"/api/user/{username}", json=kwargs)
        return normalize_vpn_user(res) if isinstance(res, dict) else res

    async def delete_user(self, username: str) -> dict:
        return await self._request("DELETE", f"/api/user/{username}")

    async def reset_usage(self, username: str) -> dict:
        return await self._request("POST", f"/api/user/{username}/reset")

    async def get_all_users(self, offset: int = 0, limit: int = 100, search: str | None = None) -> list[dict]:
        params = {"offset": offset, "limit": limit}
        if search:
            params["search"] = search
        data = await self._request("GET", "/api/users", params=params)
        users = data.get("users", []) if isinstance(data, dict) else []
        for u in users:
            normalize_vpn_user(u)
        return users

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

    async def revoke_user_subscription(self, username: str) -> dict:
        """Сбрасывает ссылку на подписку и токены, старая ссылка сразу перестает работать."""
        res = await self._request("POST", f"/api/user/{username}/revoke_sub")
        return normalize_vpn_user(res) if isinstance(res, dict) else res

    async def get_user_hwids(self, username: str) -> list[dict]:
        """Возвращает список зарегистрированных устройств (HWID) пользователя."""
        vpn_user = await self.get_user_or_none(username)
        if not vpn_user or not vpn_user.get("id"):
            return []
        pg_user_id = vpn_user["id"]
        try:
            res = await self._request("GET", f"/api/user/{pg_user_id}/hwids")
            if isinstance(res, dict):
                return res.get("hwids", []) or []
            return []
        except Exception as exc:
            logger.warning("Failed to get hwids for %s (id=%s): %s", username, pg_user_id, exc)
            return []

    async def delete_user_hwid(self, username: str, hwid: str) -> bool:
        """Удаляет конкретное устройство (HWID) пользователя."""
        vpn_user = await self.get_user_or_none(username)
        if not vpn_user or not vpn_user.get("id"):
            return False
        pg_user_id = vpn_user["id"]
        try:
            await self._request("DELETE", f"/api/user/{pg_user_id}/hwids/{hwid}")
            return True
        except Exception as exc:
            logger.warning("Failed to delete hwid %s for %s: %s", hwid, username, exc)
            return False

    async def reset_user_hwids(self, username: str) -> bool:
        """Сбрасывает все привязанные устройства пользователя."""
        vpn_user = await self.get_user_or_none(username)
        if not vpn_user or not vpn_user.get("id"):
            return False
        pg_user_id = vpn_user["id"]
        try:
            await self._request("POST", f"/api/user/{pg_user_id}/hwids/reset")
            return True
        except Exception as exc:
            logger.warning("Failed to reset hwids for %s: %s", username, exc)
            return False

    async def set_user_hwid_limit(self, username: str, limit: int | None) -> dict:
        """Устанавливает лимит устройств (HWID) в PasarGuard."""
        return await self.modify_user(username, hwid_limit=limit)

    async def get_user_online_ips(self, username: str) -> list[str]:
        """Возвращает активные IP адреса пользователя со всех нод."""
        vpn_user = await self.get_user_or_none(username)
        if not vpn_user or not vpn_user.get("id"):
            return []
        pg_user_id = vpn_user["id"]
        try:
            res = await self._request("GET", f"/api/node/online_stats/{pg_user_id}/ip")
            if isinstance(res, dict):
                return res.get("ips", []) or []
            return []
        except Exception:
            return []

    async def ensure_subscription_user(
        self,
        username: str,
        data_limit_gb: int,
        expire_days: int,
        note: str = "",
        tariff: str = "free",
        hwid_limit: int | None = None,
    ) -> dict:
        group_name = (
            settings.pasarguard_premium_group_name
            if tariff == "premium"
            else settings.pasarguard_free_group_name
        )
        try:
            group_id = await self.get_group_id_by_name(group_name)
        except Exception as exc:
            logger.warning("Could not resolve group '%s': %s — proceeding without group", group_name, exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None

        if hwid_limit is None:
            hwid_limit = settings.premium_device_limit if tariff == "premium" else settings.free_device_limit

        user = await self.get_user_or_none(username)
        if user:
            # Sync group_ids and hwid_limit if differs
            sync_kwargs: dict[str, Any] = {}
            if group_ids and user.get("group_ids") != group_ids:
                sync_kwargs["group_ids"] = group_ids
            if hwid_limit is not None and user.get("hwid_limit") != hwid_limit:
                sync_kwargs["hwid_limit"] = hwid_limit
            if sync_kwargs:
                try:
                    user = await self.modify_user(username, **sync_kwargs)
                except Exception as exc:
                    logger.warning("Could not sync for %s: %s", username, exc)
            return user

        data_limit = (data_limit_gb * (1024**3)) if data_limit_gb else 0
        await self.create_user(
            username=username,
            data_limit=data_limit,
            expire_days=expire_days,
            note=note,
            group_ids=group_ids,
            hwid_limit=hwid_limit,
        )
        return await self.get_user(username)

    async def upgrade_user_to_premium(self, username: str, days: int, hwid_limit: int | None = None) -> dict:
        try:
            group_id = await self.get_group_id_by_name(settings.pasarguard_premium_group_name)
        except Exception as exc:
            logger.warning("Could not resolve premium group: %s — proceeding without group", exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None

        if hwid_limit is None:
            hwid_limit = settings.premium_device_limit

        user = await self.get_user_or_none(username)
        now_ts = int(datetime.now(timezone.utc).timestamp())

        if not user:
            expire_ts = now_ts + (days * 86400)
            return await self.create_user(
                username=username,
                data_limit=0,
                expire=expire_ts,
                group_ids=group_ids,
                hwid_limit=hwid_limit,
                note="Premium user (auto-created)",
                status=UserStatus.ACTIVE,
            )

        current_expire = user.get("expire") or now_ts
        base_ts = max(current_expire, now_ts)
        new_expire = base_ts + (days * 86400)

        payload: dict[str, Any] = {
            "expire": new_expire,
            "status": UserStatus.ACTIVE.value,
            "data_limit": 0,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
        if hwid_limit is not None:
            payload["hwid_limit"] = hwid_limit

        updated = await self.modify_user(username, **payload)
        try:
            await self.reset_usage(username)
        except Exception:
            pass
        return updated

    async def downgrade_user_to_free(
        self,
        username: str,
        data_limit_gb: int,
        expire_days: int,
        hwid_limit: int | None = None,
    ) -> dict:
        try:
            group_id = await self.get_group_id_by_name(settings.pasarguard_free_group_name)
        except Exception as exc:
            logger.warning("Could not resolve free group: %s — proceeding without group", exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None
        now_ts = int(datetime.now(timezone.utc).timestamp())
        new_expire = now_ts + (expire_days * 86400)
        data_limit = data_limit_gb * (1024**3)

        if hwid_limit is None:
            hwid_limit = settings.free_device_limit

        payload: dict[str, Any] = {
            "expire": new_expire,
            "data_limit": data_limit,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
        if hwid_limit is not None:
            payload["hwid_limit"] = hwid_limit
        return await self.modify_user(username, **payload)

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

    async def move_user_to_renew(self, username: str) -> dict:
        """Перемещает пользователя с истекшей подпиской в группу aska_renew."""
        try:
            group_id = await self.get_group_id_by_name(settings.pasarguard_renew_group_name)
        except Exception as exc:
            logger.warning("Could not resolve renew group '%s': %s", settings.pasarguard_renew_group_name, exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None

        payload: dict[str, Any] = {
            "status": UserStatus.EXPIRED.value,
            "note": settings.expired_subscription_note,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
        return await self.modify_user(username, **payload)

    async def block_user(self, username: str) -> dict:
        """Блокирует пользователя и переносит в группу aska_blocked."""
        try:
            group_id = await self.get_group_id_by_name(settings.pasarguard_blocked_group_name)
        except Exception as exc:
            logger.warning("Could not resolve blocked group '%s': %s", settings.pasarguard_blocked_group_name, exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None

        payload: dict[str, Any] = {
            "status": UserStatus.DISABLED.value,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
        return await self.modify_user(username, **payload)

    async def unblock_user(self, username: str, tariff: str = "free") -> dict:
        """Разблокирует пользователя и возвращает в группу по его тарифу."""
        target_group = (
            settings.pasarguard_premium_group_name
            if tariff == "premium"
            else settings.pasarguard_free_group_name
        )
        try:
            group_id = await self.get_group_id_by_name(target_group)
        except Exception as exc:
            logger.warning("Could not resolve target group '%s': %s", target_group, exc)
            group_id = None
        group_ids = [group_id] if group_id is not None else None

        payload: dict[str, Any] = {
            "status": UserStatus.ACTIVE.value,
        }
        if group_ids is not None:
            payload["group_ids"] = group_ids
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


manager = PasarGuardManager()
