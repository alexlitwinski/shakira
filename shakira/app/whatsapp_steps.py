"""Envio de mensagens WhatsApp passo a passo (raciocinio visivel ao usuario)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from app.evolution import EvolutionClient

log = logging.getLogger(__name__)

WHATSAPP_TEXT_LIMIT = 3800


def truncate_whatsapp(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


@dataclass
class StepMessenger:
    """Envia cada passo como mensagem separada no WhatsApp."""

    evo: EvolutionClient
    evo_base: str
    evo_key: str
    instance: str
    phone: str
    _parts: list[str] = field(default_factory=list)

    @property
    def sent_any(self) -> bool:
        return bool(self._parts)

    def combined(self) -> str:
        return "\n\n".join(self._parts)

    async def step(self, text: str) -> None:
        msg = truncate_whatsapp((text or "").strip())
        if not msg:
            return
        if not self.evo_base or not self.evo_key or not self.instance:
            log.warning("StepMessenger: Evolution nao configurado; passo omitido")
            return
        result = await self.evo.send_text(
            base_url=self.evo_base,
            api_key=self.evo_key,
            instance=self.instance,
            number=self.phone,
            text=msg,
        )
        if result is None:
            log.warning("StepMessenger: falha ao enviar passo (%s chars)", len(msg))
        else:
            self._parts.append(msg)
            log.info("WhatsApp passo enviado phone=%s (%s chars)", self.phone, len(msg))
        delay = float(os.environ.get("WHATSAPP_STEP_DELAY_SEC", "0.55"))
        if delay > 0:
            await asyncio.sleep(delay)

    async def typing(self) -> None:
        if not self.evo_base or not self.evo_key or not self.instance:
            return
        delay_ms = int(os.environ.get("EVOLUTION_TYPING_DELAY_MS", "120000"))
        await self.evo.send_typing(
            base_url=self.evo_base,
            api_key=self.evo_key,
            instance=self.instance,
            number=self.phone,
            delay_ms=delay_ms,
        )
