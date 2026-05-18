"""Cliente Evolution API v2 (envio de texto)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class EvolutionClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send_text(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        text: str,
    ) -> dict[str, Any] | None:
        base = base_url.rstrip("/")
        url = f"{base}/message/sendText/{instance}"
        headers = {
            "Content-Type": "application/json",
            "apikey": api_key,
        }
        body = {"number": number, "text": text}
        try:
            r = await self._client.post(url, headers=headers, json=body, timeout=60.0)
        except httpx.RequestError as e:
            log.exception("Evolution send failed: %s", e)
            return None
        if r.status_code not in (200, 201):
            log.warning("Evolution API %s: %s", r.status_code, r.text[:500])
            return None
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}
