"""Registo automatico de chamadas do interfone (snapshot, Gemini, janela de atendimento)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.alerts_catalog import InterfoneDispatchConfig
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.frigate import FrigateClient, FrigateError
from app.homeassistant import HomeAssistantClient
from app.interfone_call_store import (
    InterfoneCallStore,
    configure_interfone_data_root,
    count_interfone_calls,
    get_interfone_store,
)
from app.interfone_vision import analyze_interfone_visitor

log = logging.getLogger(__name__)

STATE_ON = "on"
GATE_OPEN = "open"
PERSON_ON = frozenset({"on", "occupied", "true"})


def interfone_ringing(old_state: str | None, new_state: str | None) -> bool:
    old_on = (old_state or "").strip().lower() == STATE_ON
    new_on = (new_state or "").strip().lower() == STATE_ON
    return not old_on and new_on



@dataclass
class _ActiveCall:
    call_id: str
    started_monotonic: float
    portao_social_opened: bool = False
    portao_servico_opened: bool = False
    hall_person_detected: bool = False
    finalize_task: asyncio.Task[None] | None = None


@dataclass
class InterfoneDispatchRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    config: InterfoneDispatchConfig
    cameras: CamerasCatalog = field(default_factory=CamerasCatalog)
    http: httpx.AsyncClient | None = None
    _store: InterfoneCallStore | None = None
    _active: _ActiveCall | None = None
    _last_trigger_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reload(self, config: InterfoneDispatchConfig, *, cameras: CamerasCatalog | None = None) -> None:
        self.config = config
        if cameras is not None:
            self.cameras = cameras
        if config.data_path:
            configure_interfone_data_root(config.data_path)
        self._store = get_interfone_store(config.data_path or None)

    @property
    def watched_entity_ids(self) -> set[str]:
        return {
            self.config.interfone_entity,
            self.config.portao_social_entity,
            self.config.portao_servico_entity,
            self.config.hall_person_entity,
        }

    def _store_instance(self) -> InterfoneCallStore:
        if self._store is None:
            self._store = get_interfone_store(self.config.data_path or None)
        return self._store

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        if not self.config.enabled:
            return

        if entity_id == self.config.interfone_entity:
            if interfone_ringing(old_state, new_state):
                await self._on_interfone_ring()
            return

        active = self._active
        if not active:
            return

        state_norm = (new_state or "").strip().lower()
        updated = False

        if entity_id == self.config.portao_social_entity and state_norm == GATE_OPEN:
            if not active.portao_social_opened:
                active.portao_social_opened = True
                updated = True
                log.info("Interfone %s: portao social aberto na janela", active.call_id)
        elif entity_id == self.config.portao_servico_entity and state_norm == GATE_OPEN:
            if not active.portao_servico_opened:
                active.portao_servico_opened = True
                updated = True
                log.info("Interfone %s: portao servico aberto na janela", active.call_id)
        elif entity_id == self.config.hall_person_entity and state_norm in PERSON_ON:
            if not active.hall_person_detected:
                active.hall_person_detected = True
                updated = True
                log.info("Interfone %s: pessoa no hall na janela", active.call_id)

        if updated:
            # Sync the signals to store immediately so list_calls gets it in real-time
            store = self._store_instance()
            store.finalize_call(
                active.call_id,
                portao_social_opened=active.portao_social_opened,
                portao_servico_opened=active.portao_servico_opened,
                hall_person_detected=active.hall_person_detected,
            )

    async def _on_interfone_ring(self) -> None:
        now = time.monotonic()
        if now - self._last_trigger_at < self.config.debounce_seconds:
            log.debug("Interfone: debounce ignorou novo toque")
            return
        if self._active is not None:
            log.debug("Interfone: chamada anterior ainda em janela de atendimento")
            return

        self._last_trigger_at = now
        asyncio.create_task(self._process_call(), name="shakira-interfone-call")

    async def _process_call(self) -> None:
        import uuid
        call_id = uuid.uuid4().hex[:12]
        active = _ActiveCall(
            call_id=call_id,
            started_monotonic=time.monotonic(),
        )

        async with self._lock:
            if self._active is not None:
                return
            self._active = active

        # Começa a monitorar e faz o bootstrap de sinais imediatamente!
        await self._bootstrap_attend_signals(active)
        active.finalize_task = asyncio.create_task(
            self._finalize_after_window(active),
            name=f"shakira-interfone-window-{call_id}",
        )
        log.info("Interfone: chamada %s iniciada (janela %ss)", call_id, self.config.attend_window_seconds)

        cfg = self.config
        camera_id = cfg.camera_id.strip()
        cam = self.cameras.camera_map().get(camera_id)
        camera_label = cam.name if cam else camera_id

        image_bytes: bytes | None = None
        if self.settings.frigate_url and self.http and camera_id:
            try:
                frigate = FrigateClient(self.http, base_url=self.settings.frigate_url)
                image_bytes = await frigate.get_latest_snapshot(camera_id)
            except FrigateError as e:
                log.error("Interfone: Frigate falhou camera=%s: %s", camera_id, e)
        else:
            log.warning("Interfone: Frigate ou camera_id indisponivel")

        summary = "Chamada registada (sem imagem da câmera)."
        description = ""
        if image_bytes and self.settings.gemini_api_key:
            analysis = analyze_interfone_visitor(
                api_key=self.settings.gemini_api_key,
                image_bytes=image_bytes,
                camera_label=camera_label,
            )
            if analysis:
                summary = analysis.whatsapp_summary()
                description = analysis.visitor_description or analysis.visitor_type
        elif image_bytes:
            summary = "Chamada registada (Gemini não configurado para descrever o visitante)."

        store = self._store_instance()
        if image_bytes:
            record = store.create_call(
                camera_id=camera_id,
                image_bytes=image_bytes,
                gemini_summary=summary,
                gemini_description=description,
                attend_window_seconds=cfg.attend_window_seconds,
                call_id=call_id,
            )
        else:
            record = store.create_call(
                camera_id=camera_id,
                image_bytes=b"",
                gemini_summary=summary,
                gemini_description=description,
                attend_window_seconds=cfg.attend_window_seconds,
                call_id=call_id,
            )

        # Se algum sinal já tiver sido detectado live enquanto o Frigate/Gemini rodava,
        # persistimos imediatamente no banco de dados!
        if active.portao_social_opened or active.portao_servico_opened or active.hall_person_detected:
            store.finalize_call(
                call_id,
                portao_social_opened=active.portao_social_opened,
                portao_servico_opened=active.portao_servico_opened,
                hall_person_detected=active.hall_person_detected,
            )

    async def _bootstrap_attend_signals(self, active: _ActiveCall) -> None:
        """Marca sinais ja ativos no momento da chamada."""
        cfg = self.config
        pairs = (
            (cfg.portao_social_entity, "portao_social"),
            (cfg.portao_servico_entity, "portao_servico"),
            (cfg.hall_person_entity, "hall"),
        )
        for entity_id, kind in pairs:
            state_data = await self.ha.get_state(entity_id)
            if not state_data:
                continue
            state = str(state_data.get("state", ""))
            if kind in ("portao_social", "portao_servico") and state.strip().lower() == GATE_OPEN:
                if kind == "portao_social":
                    active.portao_social_opened = True
                else:
                    active.portao_servico_opened = True
            elif kind == "hall" and state.strip().lower() in PERSON_ON:
                active.hall_person_detected = True

    async def _finalize_after_window(self, active: _ActiveCall) -> None:
        try:
            await asyncio.sleep(self.config.attend_window_seconds)
        except asyncio.CancelledError:
            return

        store = self._store_instance()
        store.finalize_call(
            active.call_id,
            portao_social_opened=active.portao_social_opened,
            portao_servico_opened=active.portao_servico_opened,
            hall_person_detected=active.hall_person_detected,
        )
        if self._active and self._active.call_id == active.call_id:
            self._active = None
        log.info("Interfone: janela de atendimento encerrada id=%s", active.call_id)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "active_call_id": self._active.call_id if self._active else None,
            "watched_entities": sorted(self.watched_entity_ids),
            "stored_calls": count_interfone_calls(),
        }
