"""Envio de mensagens WhatsApp passo a passo (raciocinio visivel ao usuario)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from dataclasses import dataclass, field

from app.evolution import EvolutionClient
from app.user_friendly import polish_user_message

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

    async def step(self, text: str, *, final: bool = False) -> None:
        session = _typing_session.get()
        if session is None:
            await pulse_whatsapp_typing()
        msg = truncate_whatsapp(polish_user_message(text))
        if not msg:
            return
        if self._parts and self._parts[-1] == msg:
            log.info("StepMessenger: passo duplicado omitido phone=%s", self.phone)
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
        if final:
            return
        delay = float(os.environ.get("WHATSAPP_STEP_DELAY_SEC", "0.2"))
        if delay > 0:
            await asyncio.sleep(delay)

    async def typing(self) -> None:
        if not self.evo_base or not self.evo_key or not self.instance:
            return
        delay_ms = int(os.environ.get("EVOLUTION_TYPING_DELAY_MS", "15000"))
        await self.evo.send_typing(
            base_url=self.evo_base,
            api_key=self.evo_key,
            instance=self.instance,
            number=self.phone,
            delay_ms=delay_ms,
        )


class TypingSession:
    """Mantem 'digitando...' no WhatsApp enquanto o agente processa (renova ate parar)."""

    def __init__(
        self,
        evo: EvolutionClient,
        *,
        evo_base: str,
        evo_key: str,
        instance: str,
        phone: str,
    ) -> None:
        self.evo = evo
        self.evo_base = evo_base.strip()
        self.evo_key = evo_key.strip()
        self.instance = instance.strip()
        self.phone = phone
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._pulse_tasks: set[asyncio.Task[None]] = set()
        self._ctx_token: contextvars.Token[TypingSession | None] | None = None

    async def pulse(self) -> None:
        """Renova 'digitando...' sem bloquear o caller."""
        self._schedule_pulse()

    def _enabled(self) -> bool:
        return bool(self.evo_base and self.evo_key and self.instance and self.phone)

    def _schedule_pulse(self) -> None:
        if not self._enabled():
            return
        task = asyncio.create_task(self._pulse(), name="typing_pulse")
        self._pulse_tasks.add(task)
        task.add_done_callback(self._pulse_tasks.discard)

    async def _pulse(self) -> None:
        if not self._enabled():
            return
        delay_ms = int(os.environ.get("EVOLUTION_TYPING_DELAY_MS", "15000"))
        try:
            await self.evo.send_typing(
                base_url=self.evo_base,
                api_key=self.evo_key,
                instance=self.instance,
                number=self.phone,
                delay_ms=delay_ms,
            )
        except Exception:
            log.debug("TypingSession: send_typing falhou", exc_info=True)

    async def _loop(self) -> None:
        refresh_sec = float(os.environ.get("EVOLUTION_TYPING_REFRESH_SEC", "10"))
        refresh_sec = max(3.0, refresh_sec)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=refresh_sec)
            except asyncio.TimeoutError:
                if not self._stop.is_set():
                    self._schedule_pulse()

    async def __aenter__(self) -> TypingSession:
        self._ctx_token = _typing_session.set(self)
        if self._enabled():
            self._schedule_pulse()
            self._task = asyncio.create_task(self._loop(), name="typing_session")
        return self

    async def __aexit__(self, *args: object) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for pending in list(self._pulse_tasks):
            pending.cancel()
        if self._ctx_token is not None:
            _typing_session.reset(self._ctx_token)
            self._ctx_token = None
        if self._enabled():

            async def _paused() -> None:
                try:
                    await self.evo.send_paused(
                        base_url=self.evo_base,
                        api_key=self.evo_key,
                        instance=self.instance,
                        number=self.phone,
                    )
                except Exception:
                    log.debug("TypingSession: send_paused falhou", exc_info=True)

            asyncio.create_task(_paused(), name="typing_paused")


_typing_session: contextvars.ContextVar[TypingSession | None] = contextvars.ContextVar(
    "typing_session", default=None
)


async def pulse_whatsapp_typing() -> None:
    """Renova 'digitando...' se houver TypingSession ativa no contexto."""
    session = _typing_session.get()
    if session is not None:
        await session.pulse()
