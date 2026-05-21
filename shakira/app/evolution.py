"""Cliente Evolution API v2 (envio de texto e media)."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


def remote_jid_for_number(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"{digits}@s.whatsapp.net"


def parse_message_key(payload: dict[str, Any] | None, *, phone: str) -> dict[str, Any] | None:
    """Extrai key.id/remoteJid/fromMe de resposta Evolution ou record inbound."""
    if not isinstance(payload, dict):
        return None

    candidates: list[dict[str, Any]] = []
    to_visit: list[dict[str, Any]] = [payload]
    seen: set[int] = set()

    while to_visit:
        node = to_visit.pop(0)
        nid = id(node)
        if nid in seen:
            continue
        seen.add(nid)

        key = node.get("key")
        if isinstance(key, dict):
            candidates.append(key)

        message = node.get("message")
        if isinstance(message, dict):
            inner = message.get("key")
            if isinstance(inner, dict):
                candidates.append(inner)

        msg_id = node.get("messageId") or node.get("message_id")
        if msg_id and not any(c.get("id") == msg_id for c in candidates):
            node_key = node.get("key")
            remote = node.get("remoteJid") or node.get("remote_jid")
            if not remote and isinstance(node_key, dict):
                remote = node_key.get("remoteJid")
            candidates.append(
                {
                    "id": str(msg_id),
                    "remoteJid": str(remote or remote_jid_for_number(phone)),
                    "fromMe": bool(node.get("fromMe", True)),
                }
            )

        for nested_key in ("data", "result", "response"):
            nested = node.get(nested_key)
            if isinstance(nested, dict):
                to_visit.append(nested)
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        to_visit.append(item)

    for candidate in candidates:
        msg_id = candidate.get("id")
        if not msg_id:
            continue
        remote = candidate.get("remoteJid") or payload.get("remoteJid")
        if not remote:
            remote = remote_jid_for_number(phone)
        out: dict[str, Any] = {
            "id": str(msg_id),
            "remoteJid": str(remote),
            "fromMe": bool(candidate.get("fromMe", payload.get("fromMe", True))),
        }
        participant = candidate.get("participant") or payload.get("participant")
        if participant:
            out["participant"] = str(participant)
        return out
    return None


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

    async def get_media_base64(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        message_payload: dict[str, Any],
    ) -> tuple[bytes, str, str] | None:
        """Baixa midia de uma mensagem Evolution (base64). Retorna bytes, mimetype, filename."""
        base = base_url.rstrip("/")
        url = f"{base}/chat/getBase64FromMediaMessage/{instance}"
        body: dict[str, Any] = {"message": message_payload, "convertToMp4": False}
        try:
            r = await self._client.post(
                url,
                headers=self._auth_headers(api_key),
                json=body,
                timeout=120.0,
            )
        except httpx.RequestError as e:
            log.warning("Evolution getBase64FromMediaMessage failed: %s", e)
            return None
        if r.status_code not in (200, 201):
            log.warning(
                "Evolution getBase64FromMediaMessage %s: %s",
                r.status_code,
                r.text[:400],
            )
            return None
        try:
            data = r.json()
        except Exception:
            return None

        b64 = ""
        mimetype = "application/octet-stream"
        filename = "arquivo"
        if isinstance(data, dict):
            inner = data.get("base64") or data.get("data")
            if isinstance(inner, str) and inner.strip():
                b64 = inner.strip()
            elif isinstance(inner, dict):
                b64 = str(inner.get("base64") or inner.get("media") or "").strip()
                mimetype = str(inner.get("mimetype") or inner.get("mimeType") or mimetype)
                filename = str(inner.get("fileName") or inner.get("filename") or filename)
            resp = data.get("response")
            if not b64 and isinstance(resp, dict):
                b64 = str(resp.get("base64") or "").strip()
                mimetype = str(resp.get("mimetype") or resp.get("mimeType") or mimetype)
                filename = str(resp.get("fileName") or resp.get("filename") or filename)
            if not b64:
                b64 = str(data.get("media") or "").strip()
            mimetype = str(
                data.get("mimetype") or data.get("mimeType") or mimetype
            )
            filename = str(
                data.get("fileName") or data.get("filename") or filename
            )

        if not b64:
            log.warning("Evolution getBase64 sem conteudo base64")
            return None
        if b64.startswith("data:"):
            header, _, rest = b64.partition(",")
            b64 = rest
            if ";" in header:
                mt = header.split(":", 1)[-1].split(";")[0].strip()
                if mt:
                    mimetype = mt
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as e:
            log.warning("decode base64 Evolution falhou: %s", e)
            return None
        if not raw:
            return None
        return raw, mimetype, filename

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
            mediatype="image",
            mimetype=mimetype,
            filename=filename,
            caption=caption,
        )

    async def delete_message_for_everyone(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        message_id: str,
        remote_jid: str,
        from_me: bool = False,
        participant: str | None = None,
    ) -> bool:
        """Apaga mensagem enviada pelo bot para todos no chat (ex.: senha revelada)."""
        if not from_me:
            log.debug(
                "deleteMessageForEveryone ignorado: so mensagens do bot (fromMe=true)"
            )
            return False
        base = base_url.rstrip("/")
        url = f"{base}/chat/deleteMessageForEveryone/{instance}"
        body: dict[str, Any] = {
            "id": message_id,
            "remoteJid": remote_jid,
            "fromMe": from_me,
        }
        if participant:
            body["participant"] = participant
        try:
            r = await self._client.request(
                "DELETE",
                url,
                headers=self._auth_headers(api_key),
                json=body,
                timeout=30.0,
            )
        except httpx.RequestError as e:
            log.warning("Evolution deleteMessageForEveryone failed: %s", e)
            return False
        if r.status_code not in (200, 201):
            log.warning(
                "Evolution deleteMessageForEveryone %s: %s",
                r.status_code,
                r.text[:400],
            )
            return False
        return True

    async def send_document_bytes(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        number: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
        mimetype: str = "application/octet-stream",
    ) -> dict[str, Any] | None:
        b64 = base64.b64encode(file_bytes).decode("ascii")
        mediatype = "document"
        if mimetype.startswith("image/"):
            mediatype = "image"
        elif mimetype.startswith("video/"):
            mediatype = "video"
        elif mimetype.startswith("audio/"):
            mediatype = "audio"
        return await self.send_media(
            base_url=base_url,
            api_key=api_key,
            instance=instance,
            number=number,
            media=b64,
            mediatype=mediatype,
            mimetype=mimetype,
            filename=filename,
            caption=caption,
        )
