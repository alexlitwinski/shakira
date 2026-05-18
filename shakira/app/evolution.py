"""Cliente Evolution API v2 (envio de texto e media)."""

from __future__ import annotations

import base64
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

    async def send_media(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        media: str,
        mediatype: str = "image",
        mimetype: str = "image/jpeg",
        filename: str = "photo.jpg",
        caption: str = "",
    ) -> dict[str, Any] | None:
        """Envia imagem/video. media pode ser URL ou base64."""
        base = base_url.rstrip("/")
        url = f"{base}/message/sendMedia/{instance}"
        headers = {
            "Content-Type": "application/json",
            "apikey": api_key,
        }
        body: dict[str, Any] = {
            "number": number,
            "mediatype": mediatype,
            "mimetype": mimetype,
            "media": media,
            "fileName": filename,
            "caption": caption or "",
        }
        try:
            r = await self._client.post(url, headers=headers, json=body, timeout=120.0)
        except httpx.RequestError as e:
            log.exception("Evolution sendMedia failed: %s", e)
            return None
        if r.status_code not in (200, 201):
            log.warning("Evolution sendMedia %s: %s", r.status_code, r.text[:500])
            return None
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def send_image_bytes(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        image_bytes: bytes,
        filename: str = "photo.jpg",
        caption: str = "",
        mimetype: str = "image/jpeg",
    ) -> dict[str, Any] | None:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return await self.send_media(
            base_url=base_url,
            api_key=api_key,
            instance=instance,
            number=number,
            media=b64,
            mimetype=mimetype,
            filename=filename,
            caption=caption,
        )
