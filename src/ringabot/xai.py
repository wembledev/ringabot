"""Small xAI REST client: SIP call hang-up and Direct SIP phone numbers."""

from __future__ import annotations

import httpx


class XaiClient:
    def __init__(self, api_key: str, base: str = "https://api.x.ai", timeout: float = 10.0):
        self._base = base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, self._base + path, headers=self._headers, **kw)

    async def hangup(self, call_id: str) -> int:
        r = await self._req("POST", f"/v1/realtime/calls/{call_id}/hangup")
        return r.status_code

    async def list_numbers(self) -> list[dict]:
        r = await self._req("GET", "/v2/phone-numbers", params={"limit": 100})
        r.raise_for_status()
        return r.json().get("phone_numbers", [])

    async def create_number(self, body: dict) -> dict:
        r = await self._req("POST", "/v2/phone-numbers", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"create phone number failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json()
