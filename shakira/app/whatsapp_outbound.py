"""Envio proativo de mensagens WhatsApp via Evolution API."""

from __future__ import annotations

import logging
from typing import Any

from app.config import AppSettings
from app.evolution import EvolutionClient
from app.handlers import normalize_phone_digits

log = logging.getLogger(__name__)


class WhatsAppSendError(Exception):
    def __init__(self, message: str, *, status: str = "error") -> None:
        super().__init__(message)
        self.status = status


async def send_whatsapp_text(
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    number: str,
    message: str,
    instance: str | None = None,
) -> dict[str, Any]:
    phone = normalize_phone_digits(number)
    text = message.strip()
    if not phone:
        raise WhatsAppSendError("Numero invalido (use DDI+DDD+numero, somente digitos).")
    if not text:
        raise WhatsAppSendError("Mensagem vazia.")

    base = settings.evolution_base_url.strip()
    api_key = settings.evolution_api_key.strip()
    inst = (instance or settings.evolution_instance or "").strip()
    if not base or not api_key:
        raise WhatsAppSendError("Evolution API nao configurada no add-on Shakira.")
    if not inst:
        raise WhatsAppSendError("Instancia Evolution nao configurada (evolution_instance).")

    log.info("WhatsApp send >> inst=%s number=%s chars=%s", inst, phone, len(text))
    result = await evo.send_text(
        base_url=base,
        api_key=api_key,
        instance=inst,
        number=phone,
        text=text,
    )
    if result is None:
        raise WhatsAppSendError("Evolution API recusou o envio (veja logs do add-on).")
    log.info("WhatsApp send OK number=%s", phone)
    return {"ok": True, "number": phone, "instance": inst, "evolution": result}
