"""Cliente HTTP para a API REST do Home Assistant."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.config import AppSettings

log = logging.getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, settings: AppSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    @property
    def _base(self) -> str:
        return f"{self._settings.ha_url}/api"

    def _headers(self) -> dict[str, str]:
        return dict(self._settings.ha_headers)

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            encoded = quote(entity_id, safe=".")
            r = await self._client.get(
                f"{self._base}/states/{encoded}",
                headers=self._headers(),
            )
        except httpx.RequestError as e:
            log.warning("HA get_state request error %s: %s", entity_id, e)
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_states(self) -> list[dict[str, Any]]:
        r = await self._client.get(f"{self._base}/states", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        return data

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        *,
        return_response: bool = True,
    ) -> Any:
        url = f"{self._base}/services/{domain}/{service}"
        if return_response:
            url = f"{url}?return_response"
        payload = service_data or {}
        r = await self._client.post(url, headers=self._headers(), json=payload)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text
