"""Cliente HTTP para a API REST do Home Assistant."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
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
        t0 = time.monotonic()
        try:
            encoded = quote(entity_id, safe=".")
            r = await self._client.get(
                f"{self._base}/states/{encoded}",
                headers=self._headers(),
            )
        except httpx.RequestError as e:
            log.warning("HA get_state request error %s: %s", entity_id, e)
            return None
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if r.status_code == 404:
            log.debug("HA get_state %s 404 (%.0fms)", entity_id, elapsed_ms)
            return None
        r.raise_for_status()
        log.debug("HA get_state %s OK (%.0fms)", entity_id, elapsed_ms)
        return r.json()

    async def get_states(self) -> list[dict[str, Any]]:
        t0 = time.monotonic()
        r = await self._client.get(f"{self._base}/states", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if not isinstance(data, list):
            log.debug("HA get_states resposta invalida (%.0fms)", elapsed_ms)
            return []
        log.debug("HA get_states OK n=%s (%.0fms)", len(data), elapsed_ms)
        return data

    async def get_config(self) -> dict[str, Any] | None:
        """Busca as configuracoes gerais do HA (inclui lista de components/integracoes)."""
        t0 = time.monotonic()
        try:
            r = await self._client.get(f"{self._base}/config", headers=self._headers())
            r.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.debug("HA get_config OK (%.0fms)", elapsed_ms)
            return r.json()
        except Exception as e:
            log.warning("HA get_config falhou: %s", e)
            return None

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        *,
        return_response: bool = False,
    ) -> Any:
        """Chama servico HA. return_response=False por padrao (ex.: lock.unlock retorna 400 com ?return_response)."""
        url = f"{self._base}/services/{domain}/{service}"
        if return_response:
            url = f"{url}?return_response"
        payload = service_data or {}
        log.debug(
            "HA call_service >> %s/%s return_response=%s payload=%s",
            domain,
            service,
            return_response,
            payload,
        )
        try:
            r = await self._client.post(url, headers=self._headers(), json=payload)
        except httpx.RequestError as e:
            log.error("HA call_service rede falhou %s/%s: %s", domain, service, e)
            raise
        log.debug(
            "HA call_service << %s/%s status=%s body=%s",
            domain,
            service,
            r.status_code,
            (r.text or "")[:500],
        )
        if r.status_code >= 400:
            log.warning(
                "HA call_service erro %s/%s status=%s payload_enviado=%s resposta=%s",
                domain,
                service,
                r.status_code,
                payload,
                (r.text or "")[:1000],
            )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text

    async def get_history(
        self,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[list[dict[str, Any]]] | None:
        """Busca o histórico de estados de uma entidade de start_time até end_time."""
        t0 = time.monotonic()
        start_iso = start_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = end_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        
        url = f"{self._base}/history/period/{start_iso}"
        params = {
            "filter_entity_id": entity_id,
            "end_time": end_iso,
        }
        
        try:
            r = await self._client.get(
                url,
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.debug("HA get_history %s OK (%.0fms)", entity_id, elapsed_ms)
            return r.json()
        except Exception as e:
            log.warning("HA get_history falhou para %s: %s", entity_id, e)
            return None
