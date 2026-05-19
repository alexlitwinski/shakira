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

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "apikey": api_key,
        }

    async def send_presence(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        presence: str = "composing",
        delay_ms: int = 120_000,
    ) -> dict[str, Any] | None:
        """Indicador de digitando/gravando (presence composing) no WhatsApp."""
        base = base_url.rstrip("/")
        url = f"{base}/chat/sendPresence/{instance}"
        body = {
            "number": number,
            "presence": presence,
            "delay": max(1000, min(int(delay_ms), 300_000)),
        }
        try:
            r = await self._client.post(
                url,
                headers=self._auth_headers(api_key),
                json=body,
                timeout=15.0,
            )
        except httpx.RequestError as e:
            log.debug("Evolution sendPresence failed: %s", e)
            return None
        if r.status_code not in (200, 201):
            log.debug("Evolution sendPresence %s: %s", r.status_code, r.text[:300])
            return None
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def send_typing(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        delay_ms: int = 12_000,
    ) -> dict[str, Any] | None:
        return await self.send_presence(
            base_url=base_url,
            api_key=api_key,
            instance=instance,
            number=number,
            presence="composing",
            delay_ms=delay_ms,
        )

    async def send_paused(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
    ) -> dict[str, Any] | None:
        """Encerra o indicador 'digitando...' no WhatsApp."""
        return await self.send_presence(
            base_url=base_url,
            api_key=api_key,
            instance=instance,
            number=number,
            presence="paused",
            delay_ms=1000,
        )

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
        body = {"number": number, "text": text}
        try:
            r = await self._client.post(url, headers=self._auth_headers(api_key), json=body, timeout=60.0)
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
        body: dict[str, Any] = {
            "number": number,
            "mediatype": mediatype,
            "mimetype": mimetype,
            "media": media,
            "fileName": filename,
            "caption": caption or "",
        }
        try:
            r = await self._client.post(url, headers=self._auth_headers(api_key), json=body, timeout=120.0)
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
