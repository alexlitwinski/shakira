"""Executor periodico de respostas agendadas (tempo e entidade)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.conversation_history import append as history_append
from app.conversation_history import format_for_prompt, get_recent
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.homeassistant import HomeAssistantClient
from app.scheduled_response_prompts import build_fallback_message
from app.scheduled_responses import (
    MAX_ENTITY_MISS_HOURS,
    ScheduledResponse,
    ScheduledResponsesStore,
    get_scheduled_store,
    load_all_pending_globally,
    serialize_pending_item,
)
from app.state_conditions import state_matches
from app.user_friendly import (
    format_action_success,
    format_ha_error_user,
    format_state_value,
    polish_user_message,
)
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TICK_SECONDS = 30
_ENTITY_MISS_LIMIT = max(1, (MAX_ENTITY_MISS_HOURS * 3600) // TICK_SECONDS)

_runner_instance: ScheduledResponsesRunner | None = None


def set_scheduled_runner(runner: ScheduledResponsesRunner | None) -> None:
    global _runner_instance
    _runner_instance = runner


def ensure_scheduled_runner_running() -> None:
    if _runner_instance:
        _runner_instance.ensure_running()


@dataclass
class ScheduledResponsesRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    evo: EvolutionClient
    catalog: DevicesCatalog
    cameras: CamerasCatalog
    gemini_cache_name: str | None = None
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _recent_fired: dict[str, float] = field(default_factory=dict)
    _last_fired: list[dict[str, Any]] = field(default_factory=list)

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shakira-scheduled-responses")
        log.info("Executor de respostas agendadas iniciado (tick=%ss)", TICK_SECONDS)

    def ensure_running(self) -> None:
        if load_all_pending_globally():
            self.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            pending = load_all_pending_globally()
            if not pending:
                log.info("Nenhum agendamento pending — executor de respostas agendadas a parar")
                break
            try:
                await self._run_due_checks()
            except Exception:
                log.exception("Erro no ciclo de respostas agendadas")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _run_due_checks(self) -> None:
        now = datetime.now(timezone.utc)
        for entry in load_all_pending_globally():
            store = get_scheduled_store(entry.phone)
            fresh = store.get_by_id(entry.id)
            if not fresh or not fresh.is_pending():
                continue

            if self._is_expired(fresh, now):
                store.mark_expired(fresh.id)
                continue

            should_fire = await self._evaluate(fresh, store)
            if should_fire:
                if fresh.kind == "action":
                    await self._execute_action(fresh, store)
                else:
                    await self._deliver(fresh, store)

    def _is_expired(self, entry: ScheduledResponse, now: datetime) -> bool:
        if not entry.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00"))
            return now >= exp
        except ValueError:
            return False

    async def _evaluate(self, entry: ScheduledResponse, store: ScheduledResponsesStore) -> bool:
        if entry.trigger_type == "time":
            fire_at = entry.effective_fire_at()
            if fire_at is None:
                return False
            return datetime.now(timezone.utc) >= fire_at

        entity_id = (entry.entity_id or "").strip()
        when_state = (entry.when_state or "").strip()
        if not entity_id or not when_state:
            return False

        state_data = await self.ha.get_state(entity_id)
        if not state_data:
            entry.entity_miss_count += 1
            store.update(entry)
            if entry.entity_miss_count >= _ENTITY_MISS_LIMIT:
                store.mark_expired(entry.id)
                log.warning(
                    "Agendamento %s expirado: entidade %s indisponivel",
                    entry.id,
                    entity_id,
                )
            return False

        if entry.entity_miss_count:
            entry.entity_miss_count = 0

        state = str(state_data.get("state", ""))
        matched = state_matches(state, when_state)
        prev_state = entry.last_known_state
        entry.last_known_state = state
        store.update(entry)

        if entry.trigger_on == "match":
            return matched

        # enter: dispara na transicao para correspondente
        if not matched:
            return False
        if prev_state is None:
            # Primeira verificacao apos criacao: nao disparar se ja correspondia
            return False
        prev_matched = state_matches(prev_state, when_state)
        return not prev_matched

    async def _deliver(self, entry: ScheduledResponse, store: ScheduledResponsesStore) -> None:
        # Marca fired antes do envio (at-most-once)
        fired = store.mark_fired(entry.id)
        if not fired:
            return

        now_mono = time.monotonic()
        if entry.id in self._recent_fired and now_mono - self._recent_fired[entry.id] < 60:
            return
        self._recent_fired[entry.id] = now_mono

        trigger_summary = self._trigger_summary(fired)
        entity_states_block = await self._build_entity_states_block(fired)
        text = await self._generate_message(fired, trigger_summary, entity_states_block)
        if not text:
            text = build_fallback_message(
                label=fired.label,
                context=fired.context,
                trigger_summary=trigger_summary,
            )

        text = polish_user_message(text)
        try:
            await send_whatsapp_text(
                settings=self.settings,
                evo=self.evo,
                number=fired.phone,
                message=text,
            )
        except WhatsAppSendError as e:
            log.warning(
                "Agendamento %s: falha WhatsApp phone=%s: %s",
                fired.id,
                fired.phone,
                e,
            )
            return

        history_append(fired.phone, "assistant", text)
        self._last_fired.append(
            {
                "id": fired.id,
                "phone": fired.phone,
                "at": fired.fired_at,
                "label": fired.label,
            }
        )
        if len(self._last_fired) > 20:
            self._last_fired = self._last_fired[-20:]
        log.info(
            "Agendamento entregue id=%s phone=%s chars=%s",
            fired.id,
            fired.phone,
            len(text),
        )

    async def _execute_action(
        self, entry: ScheduledResponse, store: ScheduledResponsesStore
    ) -> None:
        fired = store.mark_fired(entry.id)
        if not fired:
            return

        now_mono = time.monotonic()
        if entry.id in self._recent_fired and now_mono - self._recent_fired[entry.id] < 60:
            return
        self._recent_fired[entry.id] = now_mono

        domain = (fired.action_domain or "").strip()
        service = (fired.action_service or "").strip()
        target = (fired.action_entity_id or "").strip()
        if not domain or not service or not target:
            log.warning("Agendamento acao %s incompleto", fired.id)
            return

        from app.handlers import build_ha_service_data

        svc_data = build_ha_service_data(
            domain, service, target, fired.action_service_data or None
        )
        svc_data = self.catalog.apply_service_defaults(target, svc_data)

        allowed = self.catalog.actionable_entity_ids()
        if target not in allowed:
            log.warning("Agendamento acao %s: entity %s nao acionavel", fired.id, target)
            return

        log.info(
            "Executando acao agendada id=%s phone=%s %s/%s entity=%s",
            fired.id,
            fired.phone,
            domain,
            service,
            target,
        )
        try:
            result = await self.ha.call_service(domain, service, svc_data or None)
            st_after = await self.ha.get_state(target)
            text = format_action_success(
                domain,
                service,
                target,
                svc_data,
                self.catalog,
                result=result,
                state_after=st_after,
            )
        except httpx.HTTPStatusError:
            log.exception("Acao agendada falhou id=%s entity=%s", fired.id, target)
            text = format_ha_error_user()
        except Exception:
            log.exception("Acao agendada erro id=%s entity=%s", fired.id, target)
            text = "Não consegui executar a ação agendada."

        text = polish_user_message(text)
        try:
            await send_whatsapp_text(
                settings=self.settings,
                evo=self.evo,
                number=fired.phone,
                message=text,
            )
        except WhatsAppSendError as e:
            log.warning(
                "Acao agendada %s: falha WhatsApp phone=%s: %s",
                fired.id,
                fired.phone,
                e,
            )
            return

        history_append(fired.phone, "assistant", text)
        self._last_fired.append(
            {
                "id": fired.id,
                "phone": fired.phone,
                "at": fired.fired_at,
                "label": fired.label,
                "kind": "action",
            }
        )
        if len(self._last_fired) > 20:
            self._last_fired = self._last_fired[-20:]
        log.info("Acao agendada executada id=%s phone=%s entity=%s", fired.id, fired.phone, target)

    def _trigger_summary(self, entry: ScheduledResponse) -> str:
        if entry.trigger_type == "time":
            fire = entry.effective_fire_at()
            when = fire.isoformat() if fire else "horário agendado"
            return f"Lembrete temporal disparado ({when})"
        state = entry.last_known_state or "?"
        return (
            f"Entidade {entry.entity_id} satisfez condição {entry.when_state} "
            f"(estado atual: {state})"
        )

    async def _build_entity_states_block(self, entry: ScheduledResponse) -> str:
        lines: list[str] = []
        for eid in entry.context_entities:
            st = await self.ha.get_state(eid)
            if st:
                lines.append(format_state_value(eid, st, self.catalog))
        return "\n".join(lines)

    async def _generate_message(
        self,
        entry: ScheduledResponse,
        trigger_summary: str,
        entity_states_block: str,
    ) -> str:
        if not self.settings.gemini_api_key:
            return ""

        from app.handlers import build_gemini_assistant_for_user
        from app.user_memory import get_store

        store = get_store(entry.phone)
        assistant, _, _ = build_gemini_assistant_for_user(
            self.settings,
            self.catalog,
            self.cameras,
            store,
            catalog_cache_name=self.gemini_cache_name,
        )
        history = format_for_prompt(get_recent(entry.phone)[-5:])

        def _call() -> str:
            return assistant.generate_scheduled_reply(
                context=entry.context,
                trigger_summary=trigger_summary,
                entity_states_block=entity_states_block,
                conversation_history=history,
            )

        try:
            return await asyncio.to_thread(_call)
        except Exception:
            log.exception("generate_scheduled_reply falhou id=%s", entry.id)
            return ""

    def status_snapshot(self) -> dict[str, Any]:
        pending = load_all_pending_globally()
        by_phone: dict[str, int] = {}
        for p in pending:
            by_phone[p.phone] = by_phone.get(p.phone, 0) + 1
        return {
            "running": self.is_running(),
            "tick_seconds": TICK_SECONDS,
            "pending_count": len(pending),
            "pending_by_phone": by_phone,
            "pending_items": [serialize_pending_item(p) for p in pending],
            "recent_fired": list(reversed(self._last_fired[-10:])),
        }
