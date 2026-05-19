"""Cliente HTTP para snapshots do Frigate."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class FrigateError(Exception):
    pass


class FrigateClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def probe(self, camera_id: str | None = None) -> dict[str, object]:
        """Testa conectividade com a API Frigate."""
        url = f"{self._base}/api/stats"
        try:
            r = await self._client.get(url, timeout=12.0)
        except httpx.RequestError as e:
            return {"reachable": False, "error": str(e), "url": self._base}
        return {
            "reachable": r.status_code == 200,
            "status": r.status_code,
            "url": self._base,
            "stats_ok": r.status_code == 200,
        }

    async def get_latest_snapshot(self, camera_id: str) -> bytes:
        """Obtem JPEG do frame mais recente (GET /api/{camera}/latest.jpg)."""
        url = f"{self._base}/api/{camera_id}/latest.jpg"
        log.info("Frigate snapshot >> GET %s", url)
        try:
            r = await self._client.get(url, timeout=45.0)
        except httpx.RequestError as e:
            log.exception("Frigate inacessivel: %s", e)
            raise FrigateError(f"Frigate inacessivel: {e}") from e

        ct = (r.headers.get("content-type") or "").lower()
        log.info("Frigate snapshot << status=%s ct=%s bytes=%s", r.status_code, ct, len(r.content))

        if r.status_code == 404:
            raise FrigateError(f"Camera '{camera_id}' nao encontrada no Frigate.")
        if r.status_code >= 400:
            raise FrigateError(f"Snapshot falhou: HTTP {r.status_code}")
        if not r.content:
            raise FrigateError("Snapshot vazio.")
        if "image" not in ct and not r.content.startswith(b"\xff\xd8"):
            preview = (r.text or "")[:200]
            raise FrigateError(f"Resposta inesperada do Frigate: {preview}")
        return r.content
