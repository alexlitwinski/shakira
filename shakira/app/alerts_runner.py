"""Executor periodico de alertas configurados em shakira_alerts.yaml."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.alerts_catalog import AlertConfig, AlertsCatalog
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.whatsapp_phones import (
    ENTITY_PERMITTED,
    fetch_permitted_phones_raw,
    normalize_phone_digits,
    parse_allowed_numbers,
)
from app.homeassistant import HomeAssistantClient
from app.state_conditions import state_matches
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TICK_SECONDS = 30


@dataclass
class _AlertRuntime:
    last_check_at: float = 0.0
    last_notified_at: float = 0.0
    last_matched: bool = False


@dataclass
class AlertsRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    evo: EvolutionClient
    catalog: AlertsCatalog = field(default_factory=AlertsCatalog)
    _runtimes: dict[str, _AlertRuntime] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def reload(self, catalog: AlertsCatalog) -> None:
        self.catalog = catalog
        active_ids = {a.id for a in catalog.alerts}
        for aid in list(self._runtimes.keys()):
            if aid not in active_ids:
                del self._runtimes[aid]
        log.info(
            "Alertas recarregados: %s regra(s), %s ativa(s)",
            len(catalog.alerts),
            len(catalog.enabled_alerts()),
        )

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shakira-alerts")
        log.info("Executor de alertas iniciado (tick=%ss)", TICK_SECONDS)

    def ensure_running(self) -> None:
        """Inicia o loop se houver alertas ativos."""
        if self.catalog.enabled_alerts():
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
            try:
                await self._run_due_checks()
            except Exception:
                log.exception("Erro no ciclo de alertas")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    def _runtime(self, alert_id: str) -> _AlertRuntime:
        if alert_id not in self._runtimes:
            self._runtimes[alert_id] = _AlertRuntime()
        return self._runtimes[alert_id]

    async def _resolve_phones(self, alert: AlertConfig) -> list[str]:
        configured = [
            normalize_phone_digits(p)
            for p in alert.notify.phones
            if normalize_phone_digits(p)
        ]
        if configured:
            return configured

        raw = await fetch_permitted_phones_raw(self.ha)
        return sorted(parse_allowed_numbers(raw))

    async def _run_due_checks(self) -> None:
        now = time.monotonic()
        for alert in self.catalog.enabled_alerts():
            rt = self._runtime(alert.id)
            if now - rt.last_check_at < alert.check_interval_seconds:
                continue
            rt.last_check_at = now
            await self._evaluate_alert(alert, rt, now)

    async def _evaluate_alert(self, alert: AlertConfig, rt: _AlertRuntime, now: float) -> None:
        state_data = await self.ha.get_state(alert.entity_id)
        if not state_data:
            log.warning(
                "Alerta %s: entidade %s nao encontrada no HA",
                alert.id,
                alert.entity_id,
            )
            return

        state = str(state_data.get("state", ""))
        matched = state_matches(state, alert.when_state)

        if not matched:
            if rt.last_matched:
                log.info(
                    "Alerta %s: estado %s (condicao %s) — reset cooldown",
                    alert.id,
                    state,
                    alert.when_state,
                )
            rt.last_matched = False
            return

        if rt.last_matched and rt.last_notified_at:
            elapsed = now - rt.last_notified_at
            if elapsed < alert.cooldown_seconds:
                return

        phones = await self._resolve_phones(alert)
        if not phones:
            log.warning(
                "Alerta %s: nenhum destino (configure notify.phones ou %s)",
                alert.id,
                ENTITY_PERMITTED,
            )
            return

        text = alert.message.strip()
        if "{entity_id}" in text:
            text = text.replace("{entity_id}", alert.entity_id)
        if "{state}" in text:
            text = text.replace("{state}", state)

        sent = 0
        for phone in phones:
            try:
                await send_whatsapp_text(
                    settings=self.settings,
                    evo=self.evo,
                    number=phone,
                    message=text,
                )
                sent += 1
            except WhatsAppSendError as e:
                log.warning("Alerta %s: falha WhatsApp para %s: %s", alert.id, phone, e)

        if sent:
            rt.last_notified_at = now
            rt.last_matched = True
            log.info(
                "Alerta %s disparado: entity=%s state=%s destinos=%s",
                alert.id,
                alert.entity_id,
                state,
                sent,
            )

    def status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        items: list[dict[str, Any]] = []
        for alert in self.catalog.alerts:
            rt = self._runtimes.get(alert.id)
            items.append(
                {
                    "id": alert.id,
                    "enabled": alert.enabled,
                    "entity_id": alert.entity_id,
                    "when_state": alert.when_state,
                    "check_interval_seconds": alert.check_interval_seconds,
                    "cooldown_seconds": alert.cooldown_seconds,
                    "last_check_ago_s": round(now - rt.last_check_at, 1) if rt and rt.last_check_at else None,
                    "last_notified_ago_s": round(now - rt.last_notified_at, 1)
                    if rt and rt.last_notified_at
                    else None,
                }
            )
        return {
            "running": self.is_running(),
            "tick_seconds": TICK_SECONDS,
            "alerts_count": len(self.catalog.alerts),
            "enabled_count": len(self.catalog.enabled_alerts()),
            "items": items,
        }
