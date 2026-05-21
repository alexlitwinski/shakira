"""Rotina dedicada ao disparo do alarme AMT 8000 (particoes + cameras + Gemini)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.amt_alarm_zones import (
    build_trigger_message,
    fetch_triggered_zones,
)
from app.alerts_catalog import AlarmDispatchConfig
from app.camera_snapshots import send_camera_snapshots, send_camera_vision_description
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.homeassistant import HomeAssistantClient
from app.alert_notify import resolve_notify_phones
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TRIGGERED_STATE = "triggered"
POLL_TICK_SECONDS = 30

DEFAULT_PARTITIONS: tuple[tuple[str, str], ...] = (
    ("alarm_control_panel.amt_8000_partition_1", "Partição 1 — perímetro externo"),
    ("alarm_control_panel.amt_8000_partition_2", "Partição 2 — perímetro interno"),
    ("alarm_control_panel.amt_8000_partition_3", "Partição 3 — cozinha gourmet e quarto de hóspedes"),
    ("alarm_control_panel.amt_8000_partition_4", "Partição 4 — sensores internos"),
    ("alarm_control_panel.amt_8000_partition_5", "Partição 5 — segundo andar"),
)


def resolve_camera_ids_for_partitions(
    cameras: CamerasCatalog,
    partition_entity_ids: list[str],
) -> list[str]:
    """Cameras cujo group em shakira_cameras.yaml inclui o entity_id da particao."""
    seen: set[str] = set()
    ordered: list[str] = []
    for entity_id in partition_entity_ids:
        for cam in cameras.cameras_for_group(entity_id):
            if cam.id not in seen:
                seen.add(cam.id)
                ordered.append(cam.id)
    return ordered


@dataclass
class AlarmDispatchRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    evo: EvolutionClient
    cameras: CamerasCatalog
    config: AlarmDispatchConfig
    default_notify_phones: list[str] = field(default_factory=list)
    devices: DevicesCatalog | None = None
    http: httpx.AsyncClient | None = None
    _debounce_task: asyncio.Task[None] | None = None
    _poll_task: asyncio.Task[None] | None = None
    _poll_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_known_states: dict[str, str] = field(default_factory=dict)
    _pending_partitions: set[str] = field(default_factory=set)
    _pending_zones: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    _last_notified_at: float = 0.0
    _had_triggered: bool = False
    _partitions_seeded: bool = False

    @property
    def partition_entity_ids(self) -> set[str]:
        return {eid for eid, _ in DEFAULT_PARTITIONS}

    def reload(
        self,
        config: AlarmDispatchConfig,
        *,
        cameras: CamerasCatalog | None = None,
        devices: DevicesCatalog | None = None,
    ) -> None:
        self.config = config
        if cameras is not None:
            self.cameras = cameras
        if devices is not None:
            self.devices = devices
        self._partitions_seeded = False

    async def ensure_running(self) -> None:
        if self.config.enabled:
            self._start_poll_loop()
            if not self._partitions_seeded:
                asyncio.create_task(
                    self._seed_partition_states(),
                    name="shakira-alarm-seed",
                )
        else:
            await self.stop()

    def _sector_label(self, entity_id: str) -> str:
        if self.devices:
            for device in self.devices.devices:
                for ent in device.entities:
                    if ent.entity_id == entity_id:
                        desc = (ent.description or "").strip()
                        if desc:
                            return desc
        for eid, label in DEFAULT_PARTITIONS:
            if eid == entity_id:
                return label
        return entity_id

    async def _resolve_phones(self) -> list[str]:
        return await resolve_notify_phones(
            self.ha,
            phones=self.config.notify.phones,
            default_phones=self.default_notify_phones,
        )

    async def _fetch_triggered_partitions(self) -> list[tuple[str, str]]:
        triggered: list[tuple[str, str]] = []
        for entity_id, _ in DEFAULT_PARTITIONS:
            state_data = await self.ha.get_state(entity_id)
            if not state_data:
                continue
            state = str(state_data.get("state", "")).strip().lower()
            if state == TRIGGERED_STATE:
                triggered.append((entity_id, self._sector_label(entity_id)))
        return triggered

    def start(self) -> None:
        if not self.config.enabled:
            log.info("Rotina de disparo do alarme desativada (alarm_dispatch.enabled=false)")
            return
        self._partitions_seeded = False
        self._start_poll_loop()
        asyncio.create_task(self._seed_partition_states(), name="shakira-alarm-seed")
        log.info(
            "Rotina de disparo do alarme ativa (particoes=%s, debounce=%ss, poll=%ss, WebSocket via alertas)",
            len(self.partition_entity_ids),
            self.config.debounce_seconds,
            POLL_TICK_SECONDS,
        )

    def _start_poll_loop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="shakira-alarm-dispatch-poll")

    async def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
            self._debounce_task = None

    def is_running(self) -> bool:
        return bool(
            self.config.enabled
            and self._poll_task
            and not self._poll_task.done()
        )

    async def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                await self._poll_check_partitions()
            except Exception:
                log.exception("Erro no poll de particoes do alarme")
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=POLL_TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _seed_partition_states(self) -> None:
        """Grava estado atual das particoes sem disparar aviso (arranque do add-on)."""
        for entity_id, _ in DEFAULT_PARTITIONS:
            state_data = await self.ha.get_state(entity_id)
            state = (
                str(state_data.get("state", "")).strip().lower()
                if state_data
                else ""
            )
            self._last_known_states[entity_id] = state
        self._partitions_seeded = True
        log.info(
            "Alarme: baseline de particoes no arranque (%s)",
            {eid: self._last_known_states.get(eid, "") for eid, _ in DEFAULT_PARTITIONS},
        )

    async def _poll_check_partitions(self) -> None:
        """Backup: detecta triggered via REST (util em simulacoes no HA)."""
        if not self._partitions_seeded:
            await self._seed_partition_states()
        for entity_id, _ in DEFAULT_PARTITIONS:
            state_data = await self.ha.get_state(entity_id)
            state = (
                str(state_data.get("state", "")).strip().lower()
                if state_data
                else ""
            )
            prev = self._last_known_states.get(entity_id, "")
            self._last_known_states[entity_id] = state
            if state == TRIGGERED_STATE and prev != TRIGGERED_STATE:
                log.info(
                    "Alarme (poll): particao %s entrou em triggered (antes: %s)",
                    entity_id,
                    prev or "(vazio)",
                )
                self._on_partition_triggered(entity_id)

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        if entity_id not in self.partition_entity_ids:
            return

        new_norm = (new_state or "").strip().lower()
        old_norm = (old_state or "").strip().lower() if old_state else ""

        self._last_known_states[entity_id] = new_norm

        if new_norm != TRIGGERED_STATE:
            if self._had_triggered:
                still = await self._fetch_triggered_partitions()
                if not still:
                    self._had_triggered = False
                    self._last_notified_at = 0.0
                    log.info("Alarme: todas as particoes sairam de triggered — cooldown resetado")
            return

        if old_norm == TRIGGERED_STATE:
            return

        log.info(
            "Alarme (live): particao %s entrou em triggered (antes: %s)",
            entity_id,
            old_norm or "(vazio)",
        )
        self._on_partition_triggered(entity_id)

    def _on_partition_triggered(self, entity_id: str) -> None:
        self._pending_partitions.add(entity_id)
        asyncio.create_task(
            self._capture_zones_snapshot(),
            name="shakira-alarm-zones-snapshot",
        )
        self._schedule_debounced_dispatch()

    async def _capture_zones_snapshot(self) -> None:
        try:
            for entity_id, label, state in await fetch_triggered_zones(self.ha, self.devices):
                self._pending_zones[entity_id] = (entity_id, label, state)
        except Exception:
            log.exception("Alarme: falha ao capturar snapshot de zonas")

    def _schedule_debounced_dispatch(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(self.config.debounce_seconds)
                await self._dispatch_notification()
            except asyncio.CancelledError:
                pass

        self._debounce_task = asyncio.create_task(_run(), name="shakira-alarm-dispatch")

    @staticmethod
    def _merge_triggered_zones(
        current: list[tuple[str, str, str]],
        pending: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        merged: dict[str, tuple[str, str, str]] = {}
        for row in pending + current:
            merged[row[0]] = row
        return [merged[eid] for eid in sorted(merged)]

    async def _collect_triggered_partitions(self) -> list[tuple[str, str]]:
        """
        Particoes a notificar: capturadas no evento (pending) + ainda em triggered no HA.
        Simulacoes no painel do desenvolvedor revertem o estado antes do debounce — pending resolve isso.
        """
        current = await self._fetch_triggered_partitions()
        current_ids = {eid for eid, _ in current}
        all_ids = self._pending_partitions | current_ids
        if not all_ids:
            return []
        return [(eid, self._sector_label(eid)) for eid in sorted(all_ids)]

    async def _dispatch_notification(self) -> None:
        triggered = await self._collect_triggered_partitions()
        if not triggered:
            log.info(
                "Alarme: debounce concluido sem particoes (pending=%s)",
                sorted(self._pending_partitions),
            )
            self._pending_partitions.clear()
            self._pending_zones.clear()
            return

        now = time.monotonic()
        if self._last_notified_at and now - self._last_notified_at < self.config.cooldown_seconds:
            log.info(
                "Alarme: disparo ignorado (cooldown %.0fs, particoes=%s)",
                self.config.cooldown_seconds,
                [eid for eid, _ in triggered],
            )
            return

        self._pending_partitions.clear()
        pending_zone_snapshot = dict(self._pending_zones)
        self._pending_zones.clear()
        still_active = await self._fetch_triggered_partitions()
        if still_active:
            log.info("Alarme: disparo com particoes ainda em triggered no HA")
        else:
            log.info(
                "Alarme: disparo a partir de evento (estado ja revertido no HA; particoes=%s)",
                [eid for eid, _ in triggered],
            )

        phones = await self._resolve_phones()
        if not phones:
            log.warning("Alarme: nenhum destino WhatsApp configurado")
            return

        partition_ids = [eid for eid, _ in triggered]
        triggered_zones = self._merge_triggered_zones(
            await fetch_triggered_zones(self.ha, self.devices),
            list(pending_zone_snapshot.values()),
        )
        message = build_trigger_message(triggered, triggered_zones)
        camera_ids = resolve_camera_ids_for_partitions(self.cameras, partition_ids)

        instance = (self.settings.evolution_instance or "").strip()
        if not instance:
            log.warning("Alarme: evolution_instance ausente")
            return

        sent_any = False
        for phone in phones:
            try:
                await send_whatsapp_text(
                    settings=self.settings,
                    evo=self.evo,
                    number=phone,
                    message=message,
                )
                sent_any = True
                if camera_ids and self.http:
                    await self._send_mosaic_and_analysis(
                        phone=phone,
                        instance=instance,
                        camera_ids=camera_ids,
                        context=message,
                    )
                elif not camera_ids:
                    log.warning(
                        "Alarme: nenhuma camera no catalogo para particoes %s",
                        partition_ids,
                    )
            except WhatsAppSendError as e:
                log.warning("Alarme: falha WhatsApp phone=%s: %s", phone, e)

        if sent_any:
            self._last_notified_at = now
            self._had_triggered = True
            log.info(
                "Alarme: aviso enviado particoes=%s zonas=%s cameras=%s destinos=%s",
                [label for _, label in triggered],
                [label for _, label, _ in triggered_zones],
                len(camera_ids),
                len(phones),
            )

    async def _send_mosaic_and_analysis(
        self,
        *,
        phone: str,
        instance: str,
        camera_ids: list[str],
        context: str,
    ) -> None:
        if not self.http:
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
                send_progress=False,
                send_summary=False,
            )
        except Exception:
            log.exception("Alarme: falha ao enviar mosaico phone=%s", phone)
            return

        if not self.config.describe_cameras:
            return

        await send_camera_vision_description(
            settings=self.settings,
            evo=self.evo,
            phone=phone,
            instance=instance,
            result=result,
            context=context,
        )

    def status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "enabled": self.config.enabled,
            "running": self.is_running(),
            "describe_cameras": self.config.describe_cameras,
            "cooldown_seconds": self.config.cooldown_seconds,
            "debounce_seconds": self.config.debounce_seconds,
            "poll_tick_seconds": POLL_TICK_SECONDS,
            "last_notified_ago_s": round(now - self._last_notified_at, 1)
            if self._last_notified_at
            else None,
            "partitions": [eid for eid, _ in DEFAULT_PARTITIONS],
            "last_known_states": dict(self._last_known_states),
            "websocket": {
                "mode": "shared_with_alerts_runner",
                "subscribed_entities": sorted(self.partition_entity_ids),
            },
        }
