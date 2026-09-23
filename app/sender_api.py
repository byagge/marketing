from __future__ import annotations

from typing import Any

import httpx


class SenderAPIError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SenderAPI:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.base:
            raise SenderAPIError("SENDER_API_URL не задан")
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, url, headers=self._headers(), **kwargs
            )
        if response.status_code == 401:
            raise SenderAPIError("Неверный API-ключ sender", 401)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise SenderAPIError(str(detail), response.status_code)
        if not response.content:
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base}/health")
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def list_accounts(self) -> list[dict]:
        data = await self._request("GET", "/v1/accounts")
        return data.get("accounts") or []

    async def get_account(self, account_id: str) -> dict:
        return await self._request("GET", f"/v1/accounts/{account_id}")

    async def start_account(self, account_id: str) -> dict:
        return await self._request("POST", f"/v1/accounts/{account_id}/start")

    async def create_account(
        self,
        session_bytes: bytes,
        bot_token: str,
        label: str,
        session_name: str = "account.session",
    ) -> dict:
        files = {"session": (session_name, session_bytes, "application/octet-stream")}
        data = {"bot_token": bot_token, "label": label}
        return await self._request("POST", "/v1/accounts", data=data, files=files)

    async def put_post(
        self,
        account_id: str,
        text: str,
        photo_bytes: bytes | None = None,
        *,
        mode: str = "post",
        link_chat_id: int | None = None,
        link_msg_id: int | None = None,
        entities: list[dict] | None = None,
        clear_template: bool = False,
    ) -> dict:
        # entities намеренно игнорируем — Autoposter API их не сохраняет и
        # может ответить 422 при extra=forbid.
        del entities
        payload: dict[str, Any] = {"mode": mode, "text": text}
        if photo_bytes:
            import base64

            payload["photo_base64"] = base64.b64encode(photo_bytes).decode()
            payload["photo_filename"] = "post.jpg"
        if link_chat_id is not None:
            payload["link_chat_id"] = int(link_chat_id)
        if link_msg_id is not None:
            payload["link_msg_id"] = int(link_msg_id)
        if clear_template:
            payload["clear_template"] = True
        return await self._request("PUT", f"/v1/accounts/{account_id}/post", json=payload)

    async def put_interval(self, account_id: str, kind: str, min_sec: int, max_sec: int | None = None) -> dict:
        path = {
            "between": "between",
            "cycle": "cycle",
            "per_chat": "per-chat",
            "per-chat": "per-chat",
        }[kind]
        body: dict[str, Any] = {"min_sec": int(min_sec)}
        if max_sec is not None:
            body["max_sec"] = int(max_sec)
        return await self._request("PUT", f"/v1/accounts/{account_id}/intervals/{path}", json=body)

    async def put_parallel(self, account_id: str, value: int) -> dict:
        return await self._request(
            "PUT",
            f"/v1/accounts/{account_id}/intervals/parallel",
            json={"value": int(value)},
        )

    async def put_cloak(
        self,
        account_id: str,
        enabled: bool,
        text: str,
        *,
        entities: list[dict] | None = None,
    ) -> dict:
        # entities в cloak API не пишутся — не отправляем, чтобы не словить 422
        del entities
        body: dict[str, Any] = {"enabled": bool(enabled), "text": text or ""}
        return await self._request(
            "PUT",
            f"/v1/accounts/{account_id}/cloak",
            json=body,
        )

    async def list_chats(self, account_id: str) -> list[dict]:
        data = await self._request("GET", f"/v1/accounts/{account_id}/chats")
        return data.get("chats") or []

    async def patch_chat(self, account_id: str, chat_id: str, **fields: Any) -> dict:
        return await self._request(
            "PATCH",
            f"/v1/accounts/{account_id}/chats/{chat_id}",
            json=fields,
        )

    async def spam_start(self, account_id: str) -> dict:
        return await self._request("POST", f"/v1/accounts/{account_id}/spam/start")

    async def spam_stop(self, account_id: str) -> dict:
        return await self._request("POST", f"/v1/accounts/{account_id}/spam/stop")
