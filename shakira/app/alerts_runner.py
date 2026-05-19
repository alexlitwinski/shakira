"""Executor de alertas (polling periodico + live via WebSocket HA)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.alerts_catalog import AlertConfig, AlertsCatalog, live_alerts
from app.camera_snapshots import send_camera_snapshots
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.ha_websocket import HaWebSocketListener
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
    cameras: CamerasCatalog = field(default_factory=CamerasCatalog)
    http: httpx.AsyncClient | None = None
    _runtimes: dict[str, _AlertRuntime] = field(default_factory=dict)
    _poll_task: asyncio.Task[None] | None = None
    _poll_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _ws_listener: HaWebSocketListener | None = None
    _ws_bootstrap_done: bool = False

    def reload(self, catalog: AlertsCatalog) -> None:
        self.catalog = catalog
        active_ids = {a.id for a in catalog.alerts}
        for aid in list(self._runtimes.keys()):
            if aid not in active_ids:
                del self._runtimes[aid]
        self._sync_websocket_entities()
        live_count = len(catalog.enabled_live_alerts())
        poll_count = len(catalog.enabled_polling_alerts())
        log.info(
            "Alertas recarregados: %s regra(s), %s ativa(s) (%s live, %s polling)",
            len(catalog.alerts),
            len(catalog.enabled_alerts()),
            live_count,
            poll_count,
        )

    def is_running(self) -> bool:
        poll = bool(self._poll_task and not self._poll_task.done())
        ws = bool(self._ws_listener and self._ws_listener._task and not self._ws_listener._task.done())
        return poll or ws

    def _live_entity_ids(self) -> set[str]:
        return {a.entity_id for a in self.catalog.enabled_live_alerts()}

    def _sync_websocket_entities(self) -> None:
        entity_ids = self._live_entity_ids()
        if self._ws_listener:
            self._ws_listener.update_entity_ids(entity_ids)

    def start(self) -> None:
        if self.catalog.enabled_polling_alerts() and not (
            self._poll_task and not self._poll_task.done()
        ):
            self._poll_stop.clear()
            self._poll_task = asyncio.create_task(self._poll_loop(), name="shakira-alerts-poll")
            log.info("Executor de alertas polling iniciado (tick=%ss)", TICK_SECONDS)

        if self.catalog.enabled_live_alerts():
            self._start_websocket()

    async def ensure_running(self) -> None:
        """Inicia polling e/ou WebSocket se houver alertas activos."""
        if self.catalog.enabled_polling_alerts():
            if not (self._poll_task and not self._poll_task.done()):
                self._poll_stop.clear()
                self._poll_task = asyncio.create_task(
                    self._poll_loop(), name="shakira-alerts-poll"
                )
                log.info("Executor de alertas polling iniciado (tick=%ss)", TICK_SECONDS)
        elif self._poll_task and not self._poll_task.done():
            self._poll_stop.set()

        if self.catalog.enabled_live_alerts():
            self._start_websocket()
        else:
            await self._stop_websocket()

    def _start_websocket(self) -> None:
        entity_ids = self._live_entity_ids()
        if not entity_ids:
            return

        if self._ws_listener is None:
            self._ws_listener = HaWebSocketListener(
                settings=self.settings,
                on_state_changed=self.handle_live_state_change,
                entity_ids=entity_ids,
            )
        else:
            self._ws_listener.update_entity_ids(entity_ids)

        if not (self._ws_listener._task and not self._ws_listener._task.done()):
            self._ws_bootstrap_done = False
            self._ws_listener.start()
            asyncio.create_task(self._bootstrap_when_connected())

    async def _bootstrap_when_connected(self) -> None:
        """Aguarda conexao WS e faz bootstrap dos alertas live."""
        if self._ws_bootstrap_done or not self._ws_listener:
            return
        for _ in range(60):
            if self._ws_listener.is_connected():
                await self.bootstrap_live_alerts()
                self._ws_bootstrap_done = True
                return
            await asyncio.sleep(0.5)
        log.warning("Bootstrap alertas live: WebSocket nao conectou a tempo")

    async def _stop_websocket(self) -> None:
        if self._ws_listener:
            await self._ws_listener.stop()
            self._ws_listener = None
            self._ws_bootstrap_done = False

    async def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._stop_websocket()

    async def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                await self._run_due_checks()
            except Exception:
                log.exception("Erro no ciclo de alertas polling")
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=TICK_SECONDS)
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

    def _resolve_alert_camera_ids(self, alert: AlertConfig) -> list[str]:
        group = alert.camera_group.strip()
        if not group:
            return []
        ids, err = self.cameras.resolve_camera_targets(camera_group=group)
        if err or not ids:
            log.warning(
                "Alerta %s: camera_group '%s' invalido: %s",
                alert.id,
                group,
                err or "sem cameras",
            )
            return []
        return ids

    async def _send_alert_camera_snapshots(
        self, alert: AlertConfig, phone: str, camera_ids: list[str]
    ) -> None:
        if not camera_ids or not self.http:
            return
        instance = (self.settings.evolution_instance or "").strip()
        if not instance:
            log.warning("Alerta %s: evolution_instance ausente para envio de cameras", alert.id)
            return
        try:
            result = await send_camera_snapshots(
                settings=self.settings,
                cameras=self.cameras,
                evo=self.evo,
                http=self.http,
                phone=phone,
                instance=instance,
                camera_ids=camera_ids,
            )
            log.info(
                "Alerta %s: cameras grupo '%s' phone=%s enviadas=%s falhas=%s",
                alert.id,
                alert.camera_group,
                phone,
                result.sent,
                result.failed,
            )
        except Exception:
            log.exception("Alerta %s: falha ao enviar cameras phone=%s", alert.id, phone)

    async def _run_due_checks(self) -> None:
        now = time.monotonic()
        for alert in self.catalog.enabled_polling_alerts():
            rt = self._runtime(alert.id)
            if now - rt.last_check_at < alert.check_interval_seconds:
                continue
            rt.last_check_at = now
            await self._evaluate_alert(alert, rt, now)

    async def bootstrap_live_alerts(self) -> None:
        """Avalia alertas live ao conectar WebSocket (dispara se ja em alerta)."""
        now = time.monotonic()
        for alert in live_alerts(self.catalog):
            await self._evaluate_alert(alert, self._runtime(alert.id), now)

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        now = time.monotonic()
        for alert in self.catalog.enabled_live_alerts():
            if alert.entity_id != entity_id:
                continue
            await self._evaluate_alert(
                alert,
                self._runtime(alert.id),
                now,
                state_override=new_state,
            )

    async def _evaluate_alert(
        self,
        alert: AlertConfig,
        rt: _AlertRuntime,
        now: float,
        *,
        state_override: str | None = None,
    ) -> None:
        if state_override is not None:
            state = state_override
        else:
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

        camera_ids = self._resolve_alert_camera_ids(alert)
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
                if camera_ids:
                    await self._send_alert_camera_snapshots(alert, phone, camera_ids)
            except WhatsAppSendError as e:
                log.warning("Alerta %s: falha WhatsApp para %s: %s", alert.id, phone, e)

        if sent:
            rt.last_notified_at = now
            rt.last_matched = True
            mode = "live" if alert.live else "polling"
            log.info(
                "Alerta %s (%s) disparado: entity=%s state=%s destinos=%s",
                alert.id,
                mode,
                alert.entity_id,
                state,
                sent,
            )
            await self._schedule_recovery_notifications(alert, phones, state)

    async def _schedule_recovery_notifications(
        self,
        alert: AlertConfig,
        phones: list[str],
        current_state: str,
    ) -> None:
        """Agenda resposta do agente quando a entidade voltar ao normal."""
        if not alert.recovery_when_state or not alert.recovery_context:
            return

        from app.scheduled_responses import ensure_runner_started, get_scheduled_store

        label = alert.recovery_label.strip() or f"alerta_{alert.id}_ok"
        scheduled = 0
        for phone in phones:
            store = get_scheduled_store(phone)
            if store.find_by_label(label):
                continue
            try:
                store.add(
                    context=alert.recovery_context,
                    trigger_type="entity",
                    label=label,
                    entity_id=alert.entity_id,
                    when_state=alert.recovery_when_state,
                    trigger_on="enter",
                    context_entities=[alert.entity_id],
                    last_known_state=current_state,
                )
                scheduled += 1
            except ValueError as e:
                log.warning(
                    "Alerta %s: nao agendou recuperacao phone=%s: %s",
                    alert.id,
                    phone,
                    e,
                )
        if scheduled:
            ensure_runner_started()
            log.info(
                "Alerta %s: %s resposta(s) agendada(s) para recuperacao (%s)",
                alert.id,
                scheduled,
                alert.recovery_when_state,
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
                    "live": alert.live,
                    "entity_id": alert.entity_id,
                    "when_state": alert.when_state,
                    "check_interval_seconds": None if alert.live else alert.check_interval_seconds,
                    "cooldown_seconds": alert.cooldown_seconds,
                    "camera_group": alert.camera_group or None,
                    "last_check_ago_s": round(now - rt.last_check_at, 1) if rt and rt.last_check_at else None,
                    "last_notified_ago_s": round(now - rt.last_notified_at, 1)
                    if rt and rt.last_notified_at
                    else None,
                }
            )
        ws_status: dict[str, Any] = {
            "connected": False,
            "reconnect_attempts": 0,
            "last_event_at": None,
            "subscribed_entities": sorted(self._live_entity_ids()),
        }
        if self._ws_listener:
            ws_status = self._ws_listener.status_snapshot()

        poll_running = bool(self._poll_task and not self._poll_task.done())
        ws_running = bool(
            self._ws_listener
            and self._ws_listener._task
            and not self._ws_listener._task.done()
        )

        return {
            "running": poll_running or ws_running,
            "polling_running": poll_running,
            "websocket_running": ws_running,
            "tick_seconds": TICK_SECONDS,
            "alerts_count": len(self.catalog.alerts),
            "enabled_count": len(self.catalog.enabled_alerts()),
            "polling_enabled_count": len(self.catalog.enabled_polling_alerts()),
            "live_enabled_count": len(self.catalog.enabled_live_alerts()),
            "websocket": ws_status,
            "items": items,
        }
