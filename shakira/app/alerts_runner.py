"""Executor de alertas (polling periodico + live via WebSocket HA)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.alarm_dispatch_runner import AlarmDispatchRunner
    from app.interfone_dispatch_runner import InterfoneDispatchRunner
from app.rain_dispatch_runner import RainDispatchRunner
from app.presence_simulator_runner import PresenceSimulatorRunner

import httpx

from app.alerts_catalog import AlertConfig, AlertsCatalog
from app.camera_snapshots import send_camera_snapshots
from app.camera_vision import (
    CameraPanelInfo,
    DEFAULT_MAX_VISION_RETRIES,
    DEFAULT_RETRY_DELAY_SECONDS,
    analyze_camera_mosaic,
    build_retry_notice,
    format_analysis_message,
    should_retry_for_missing_person,
)
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.ha_websocket import HaWebSocketListener
from app.alert_notify import permitted_entity_hint, resolve_notify_phones
from app.homeassistant import HomeAssistantClient
from app.state_conditions import state_matches
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TICK_SECONDS = 30
ALARM_TRIGGERED_STATE = "triggered"

VIBRATION_RULES = [
    {
        "sensor": "binary_sensor.sensor_vibracao_portao_social_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_1",
        "config": "input_boolean.monitorar_vibracao_no_portao_social",
        "siren": "switch.sirene_entrada_alarm",
        "volume": "number.sirene_entrada_volume",
        "name": "Portão Social"
    },
    {
        "sensor": "binary_sensor.vibracao_janela_sala_1_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_1",
        "config": "input_boolean.monitorar_vibracao_nas_janelas_da_sala",
        "siren": "switch.sirene_despensa_alarm",
        "volume": "number.sirene_despensa_volume",
        "name": "Janela da Sala 1"
    },
    {
        "sensor": "binary_sensor.vibracao_janela_sala_2_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_1",
        "config": "input_boolean.monitorar_vibracao_nas_janelas_da_sala",
        "siren": "switch.sirene_despensa_alarm",
        "volume": "number.sirene_despensa_volume",
        "name": "Janela da Sala 2"
    },
    {
        "sensor": "binary_sensor.vibracao_janela_sala_3_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_1",
        "config": "input_boolean.monitorar_vibracao_nas_janelas_da_sala",
        "siren": "switch.sirene_despensa_alarm",
        "volume": "number.sirene_despensa_volume",
        "name": "Janela da Sala 3"
    },
    {
        "sensor": "binary_sensor.vibracao_porta_da_hanna_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_5",
        "config": "input_boolean.monitorar_vibracao_na_porta_da_hanna",
        "siren": "switch.sirene_hanna_alarm",
        "volume": "number.sirene_hanna_volume",
        "name": "Porta da Hanna"
    },
    {
        "sensor": "binary_sensor.vibracao_portao_garagem_vibration",
        "partition": "alarm_control_panel.amt_8000_partition_1",
        "config": None,
        "siren": "switch.sirene_garagem_alarm",
        "volume": "number.sirene_garagem_volume",
        "name": "Portão da Garagem"
    }
]


def should_describe_alert_cameras(alert: AlertConfig) -> bool:
    """True se o alerta pede descricao Gemini (explicita ou disparo de particao)."""
    if alert.describe_cameras:
        return True
    return (
        alert.entity_id.startswith("alarm_control_panel.")
        and alert.when_state.strip().lower() == ALARM_TRIGGERED_STATE
    )


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
    _alarm_dispatch: AlarmDispatchRunner | None = None
    _rain_dispatch: RainDispatchRunner | None = None
    _interfone_dispatch: InterfoneDispatchRunner | None = None
    _presence_simulator: PresenceSimulatorRunner | None = None

    def attach_presence_simulator(self, runner: PresenceSimulatorRunner | None) -> None:
        """Partilha o WebSocket live com a rotina de simulação de presença."""
        self._presence_simulator = runner
        self._sync_websocket_entities()

    def attach_alarm_dispatch(self, runner: AlarmDispatchRunner | None) -> None:
        """Partilha o WebSocket live com a rotina de disparo do alarme."""
        self._alarm_dispatch = runner
        self._sync_websocket_entities()

    def attach_rain_dispatch(self, runner: RainDispatchRunner | None) -> None:
        """Partilha o WebSocket live com a rotina de chuva."""
        self._rain_dispatch = runner
        self._sync_websocket_entities()

    def attach_interfone_dispatch(self, runner: InterfoneDispatchRunner | None) -> None:
        """Partilha o WebSocket live com a rotina de chamadas do interfone."""
        self._interfone_dispatch = runner
        self._sync_websocket_entities()

    def reload(self, catalog: AlertsCatalog) -> None:
        self.catalog = catalog
        active_ids = {a.id for a in catalog.alerts}
        for aid in list(self._runtimes.keys()):
            if aid not in active_ids:
                del self._runtimes[aid]
        self._seed_polling_baseline()
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

    def _alarm_dispatch_active(self) -> bool:
        return bool(
            self._alarm_dispatch
            and self._alarm_dispatch.config.enabled
        )

    def _rain_dispatch_active(self) -> bool:
        return bool(
            self._rain_dispatch
            and self._rain_dispatch.config.enabled
        )

    def _interfone_dispatch_active(self) -> bool:
        return bool(
            self._interfone_dispatch
            and self._interfone_dispatch.config.enabled
        )

    def _presence_simulator_active(self) -> bool:
        return bool(
            self._presence_simulator
            and self._presence_simulator.config.enabled
        )

    def _live_entity_ids(self) -> set[str]:
        ids = {a.entity_id for a in self.catalog.enabled_live_alerts()}
        if any(a.id == "presenca_portao_parado" and a.enabled for a in self.catalog.alerts):
            ids.add("sensor.presenca_porta_vidro_target_distance")
            ids.add("binary_sensor.presenca_porta_vidro_presence")
        if self._alarm_dispatch_active():
            ids |= self._alarm_dispatch.partition_entity_ids  # type: ignore[union-attr]
        if self._rain_dispatch_active():
            ids |= self._rain_dispatch.watched_entity_ids  # type: ignore[union-attr]
        if self._interfone_dispatch_active():
            ids |= self._interfone_dispatch.watched_entity_ids  # type: ignore[union-attr]
        if self._presence_simulator_active():
            ids.add(self._presence_simulator.config.control_entity)  # type: ignore[union-attr]

        # Monitora o aquecimento das bombas de fumaça em tempo real
        ids.add("switch.bomba_fumaca_garagem_aquecimento")
        ids.add("switch.bomba_fumaca_sala_aquecimento")
        ids.add("switch.bomba_fumaca_despenca_aquecimento")

        # Monitora permanentemente a presença na rua para fins de estatística
        ids.add("binary_sensor.presenca_porta_vidro_presence")

        # Monitora permanentemente as entidades da rotina de vibração
        vibration_sensors = {
            "binary_sensor.sensor_vibracao_portao_social_vibration",
            "binary_sensor.vibracao_janela_sala_1_vibration",
            "binary_sensor.vibracao_janela_sala_2_vibration",
            "binary_sensor.vibracao_janela_sala_3_vibration",
            "binary_sensor.vibracao_porta_da_hanna_vibration",
            "binary_sensor.vibracao_portao_garagem_vibration",
        }
        vibration_configs = {
            "input_boolean.monitorar_vibracao_no_portao_social",
            "input_boolean.monitorar_vibracao_nas_janelas_da_sala",
            "input_boolean.monitorar_vibracao_na_porta_da_hanna",
        }
        vibration_partitions = {
            "alarm_control_panel.amt_8000_partition_1",
            "alarm_control_panel.amt_8000_partition_5",
        }
        ids |= vibration_sensors | vibration_configs | vibration_partitions

        return ids

    def _needs_live_websocket(self) -> bool:
        return (
            bool(self.catalog.enabled_live_alerts())
            or self._alarm_dispatch_active()
            or self._rain_dispatch_active()
            or self._interfone_dispatch_active()
            or self._presence_simulator_active()
        )

    def _sync_websocket_entities(self) -> None:
        entity_ids = self._live_entity_ids()
        if self._ws_listener:
            self._ws_listener.update_entity_ids(entity_ids)

    def _seed_polling_baseline(self) -> None:
        """Evita disparar alertas de polling no primeiro ciclo apos arranque/reload."""
        now = time.monotonic()
        for alert in self.catalog.enabled_polling_alerts():
            self._runtime(alert.id).last_check_at = now

    @property
    def street_store(self) -> Any:
        if not hasattr(self, "_street_presence_store"):
            from app.street_presence_store import StreetPresenceStore
            self._street_presence_store = StreetPresenceStore()
        return self._street_presence_store

    async def _sync_street_presence_history(self) -> None:
        try:
            # Aguarda um tempinho curto para não travar a inicialização imediata
            await asyncio.sleep(2.0)
            await self.street_store.sync_history_from_ha(self.ha)
        except Exception as e:
            log.warning("Falha na sincronização inicial do histórico da rua: %s", e)

    def start(self) -> None:
        self._seed_polling_baseline()
        if self.catalog.enabled_polling_alerts() and not (
            self._poll_task and not self._poll_task.done()
        ):
            self._poll_stop.clear()
            self._poll_task = asyncio.create_task(self._poll_loop(), name="shakira-alerts-poll")
            log.info("Executor de alertas polling iniciado (tick=%ss)", TICK_SECONDS)

        if self._needs_live_websocket():
            self._start_websocket()

        # Sincroniza retroativamente o histórico de movimentação na rua em background
        asyncio.create_task(
            self._sync_street_presence_history(),
            name="shakira-street-presence-sync",
        )

    async def ensure_running(self) -> None:
        """Inicia polling e/ou WebSocket se houver alertas activos."""
        if self.catalog.enabled_polling_alerts():
            if not (self._poll_task and not self._poll_task.done()):
                self._seed_polling_baseline()
                self._poll_stop.clear()
                self._poll_task = asyncio.create_task(
                    self._poll_loop(), name="shakira-alerts-poll"
                )
                log.info("Executor de alertas polling iniciado (tick=%ss)", TICK_SECONDS)
        elif self._poll_task and not self._poll_task.done():
            self._poll_stop.set()

        if self._needs_live_websocket():
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
            self._ws_listener.start()

    async def _stop_websocket(self) -> None:
        if self._ws_listener:
            await self._ws_listener.stop()
            self._ws_listener = None

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
                await self._check_presenca_portao_tick()
                await self._check_smoke_bombs_heating_timeout()
            except Exception:
                log.exception("Erro no ciclo de alertas polling")
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _check_smoke_bombs_heating_timeout(self) -> None:
        """Verifica se qualquer bomba de fumaça está com o aquecimento ativo por mais de 15 minutos e desliga."""
        entities = [
            "switch.bomba_fumaca_garagem_aquecimento",
            "switch.bomba_fumaca_sala_aquecimento",
            "switch.bomba_fumaca_despenca_aquecimento",
        ]
        
        for entity_id in entities:
            try:
                state_data = await self.ha.get_state(entity_id)
                if not state_data:
                    continue
                    
                state = str(state_data.get("state", "")).strip().lower()
                if state == "on":
                    last_changed_str = state_data.get("last_changed")
                    if last_changed_str:
                        if last_changed_str.endswith("Z"):
                            last_changed_str = last_changed_str[:-1] + "+00:00"
                        
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(last_changed_str)
                        now_dt = datetime.now(timezone.utc)
                        duration_seconds = (now_dt - dt).total_seconds()
                        
                        # 15 minutos (900 segundos)
                        if duration_seconds > 900.0:
                            log.warning(
                                "Bomba fumaça: %s está ligada há %.1f segundos (> 15 min). Desligando automaticamente.",
                                entity_id,
                                duration_seconds,
                            )
                            # Registra no set para evitar aviso genérico de "DESLIGADO"
                            if not hasattr(self, "_smoke_bombs_timeout_triggered"):
                                self._smoke_bombs_timeout_triggered = set()
                            self._smoke_bombs_timeout_triggered.add(entity_id)

                            # Desliga via HA
                            await self.ha.call_service("switch", "turn_off", {"entity_id": entity_id})
                            
                            # Envia notificação por WhatsApp
                            labels = {
                                "switch.bomba_fumaca_garagem_aquecimento": "Garagem",
                                "switch.bomba_fumaca_sala_aquecimento": "Sala",
                                "switch.bomba_fumaca_despenca_aquecimento": "Despensa",
                            }
                            room = labels.get(entity_id, entity_id)
                            msg = f"⏱️ *AVISO:* O aquecimento da bomba de fumaça da *{room}* excedeu o limite máximo de 15 minutos e foi *DESLIGADO automaticamente*."
                            
                            phones = await resolve_notify_phones(
                                self.ha,
                                phones=[],
                                default_phones=self.catalog.default_notify.phones,
                            )
                            for phone in phones:
                                try:
                                    await send_whatsapp_text(
                                        settings=self.settings,
                                        evo=self.evo,
                                        number=phone,
                                        message=msg,
                                    )
                                except Exception:
                                    pass
            except Exception as e:
                log.warning("Erro ao verificar timeout da bomba de fumaça %s: %s", entity_id, e)

    async def _check_presenca_portao_tick(self) -> None:
        alert = next((a for a in self.catalog.alerts if a.id == "presenca_portao_parado" and a.enabled), None)
        if not alert:
            return
        rt = self._runtime(alert.id)
        now = time.monotonic()
        await self._evaluate_alert(alert, rt, now)

    def _runtime(self, alert_id: str) -> _AlertRuntime:
        if alert_id not in self._runtimes:
            self._runtimes[alert_id] = _AlertRuntime(last_check_at=time.monotonic())
        return self._runtimes[alert_id]

    async def _resolve_phones(self, alert: AlertConfig) -> list[str]:
        return await resolve_notify_phones(
            self.ha,
            phones=alert.notify.phones,
            default_phones=self.catalog.default_notify.phones,
        )

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
        self, alert: AlertConfig, phone: str, camera_ids: list[str], *, context: str = ""
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
            if should_describe_alert_cameras(alert) and result.image_bytes and result.sent > 0:
                await self._send_camera_description(
                    alert=alert,
                    phone=phone,
                    camera_ids=camera_ids,
                    image_bytes=result.image_bytes,
                    camera_panels=result.image_panels,
                    context=context,
                )
        except Exception:
            log.exception("Alerta %s: falha ao enviar cameras phone=%s", alert.id, phone)

    async def _send_camera_description(
        self,
        *,
        alert: AlertConfig,
        phone: str,
        camera_ids: list[str],
        image_bytes: bytes,
        camera_panels: list[CameraPanelInfo],
        context: str,
    ) -> None:
        api_key = self.settings.gemini_api_key.strip()
        if not api_key:
            log.warning(
                "Alerta %s: describe_cameras ativo mas gemini_api_key ausente",
                alert.id,
            )
            return

        watch_names = alert.describe_cameras_watch or None
        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        instance = (self.settings.evolution_instance or "").strip()
        max_retries = DEFAULT_MAX_VISION_RETRIES

        current_bytes = image_bytes
        current_panels = camera_panels

        for attempt in range(max_retries + 1):
            try:
                analysis = await asyncio.to_thread(
                    analyze_camera_mosaic,
                    api_key=api_key,
                    image_bytes=current_bytes,
                    camera_panels=current_panels,
                    context=context,
                    model=model,
                )
            except Exception:
                log.exception("Alerta %s: falha Gemini vision phone=%s", alert.id, phone)
                return

            if analysis is None:
                log.warning("Alerta %s: Gemini vision retornou vazio phone=%s", alert.id, phone)
                return

            needs_retry = (
                attempt < max_retries
                and should_retry_for_missing_person(
                    analysis,
                    current_panels,
                    watch_names,
                )
            )
            if needs_retry:
                log.info(
                    "Alerta %s: sem pessoa nas cameras monitoradas; retry %s/%s phone=%s",
                    alert.id,
                    attempt + 1,
                    max_retries,
                    phone,
                )
                try:
                    await send_whatsapp_text(
                        settings=self.settings,
                        evo=self.evo,
                        number=phone,
                        message=build_retry_notice(watch_names),
                    )
                except WhatsAppSendError as e:
                    log.warning(
                        "Alerta %s: falha WhatsApp aviso retry phone=%s: %s",
                        alert.id,
                        phone,
                        e,
                    )

                await asyncio.sleep(DEFAULT_RETRY_DELAY_SECONDS)

                if not self.http or not instance:
                    log.warning(
                        "Alerta %s: retry abortado (http ou evolution ausente)",
                        alert.id,
                    )
                    return

                try:
                    retry_result = await send_camera_snapshots(
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
                    log.exception(
                        "Alerta %s: falha ao recapturar cameras phone=%s",
                        alert.id,
                        phone,
                    )
                    return

                if not retry_result.image_bytes or retry_result.sent <= 0:
                    log.warning(
                        "Alerta %s: retry sem imagem valida phone=%s",
                        alert.id,
                        phone,
                    )
                    return

                current_bytes = retry_result.image_bytes
                current_panels = retry_result.image_panels
                continue

            description = format_analysis_message(analysis)
            if not description:
                log.warning(
                    "Alerta %s: analise Gemini sem texto phone=%s",
                    alert.id,
                    phone,
                )
                return

            try:
                await send_whatsapp_text(
                    settings=self.settings,
                    evo=self.evo,
                    number=phone,
                    message=description,
                )
                log.info(
                    "Alerta %s: descricao Gemini enviada phone=%s chars=%s attempt=%s",
                    alert.id,
                    phone,
                    len(description),
                    attempt + 1,
                )
            except WhatsAppSendError as e:
                log.warning(
                    "Alerta %s: falha WhatsApp descricao cameras phone=%s: %s",
                    alert.id,
                    phone,
                    e,
                )
            return

    async def _run_due_checks(self) -> None:
        now = time.monotonic()
        for alert in self.catalog.enabled_polling_alerts():
            rt = self._runtime(alert.id)
            if now - rt.last_check_at < alert.check_interval_seconds:
                continue
            rt.last_check_at = now
            await self._evaluate_alert(alert, rt, now)

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        """Dispara alertas live apenas em transicao real para o estado de alerta."""
        now = time.monotonic()

        # Intercepta eventos de vibração na casa em tempo real
        vibration_sensors = {
            "binary_sensor.sensor_vibracao_portao_social_vibration",
            "binary_sensor.vibracao_janela_sala_1_vibration",
            "binary_sensor.vibracao_janela_sala_2_vibration",
            "binary_sensor.vibracao_janela_sala_3_vibration",
            "binary_sensor.vibracao_porta_da_hanna_vibration",
            "binary_sensor.vibracao_portao_garagem_vibration",
        }
        if entity_id in vibration_sensors:
            new_norm = (new_state or "").strip().lower()
            old_norm = (old_state or "").strip().lower() if old_state else ""
            if new_norm == "on" and old_norm != "on":
                asyncio.create_task(
                    self._evaluate_vibration_alert(entity_id),
                    name=f"shakira-vibration-{entity_id}"
                )

        # Registra eventos de movimentação na rua no banco SQLite local
        if entity_id == "binary_sensor.presenca_porta_vidro_presence":
            new_norm = (new_state or "").strip().lower()
            old_norm = (old_state or "").strip().lower() if old_state else ""
            if new_norm == "on" and old_norm != "on":
                from datetime import datetime, timezone
                event_time = None
                if _event_data:
                    new_state_dict = _event_data.get("new_state")
                    if isinstance(new_state_dict, dict):
                        last_changed_str = new_state_dict.get("last_changed")
                        if last_changed_str:
                            try:
                                if last_changed_str.endswith("Z"):
                                    last_changed_str = last_changed_str[:-1] + "+00:00"
                                event_time = datetime.fromisoformat(last_changed_str)
                            except Exception:
                                pass
                if not event_time:
                    event_time = datetime.now(timezone.utc)
                
                # Registra o evento na store
                self.street_store.register_event(event_time, "on")

        if entity_id in ("binary_sensor.presenca_porta_vidro_presence", "sensor.presenca_porta_vidro_target_distance"):
            alert = next((a for a in self.catalog.alerts if a.id == "presenca_portao_parado" and a.enabled), None)
            if alert:
                rt = self._runtime(alert.id)
                await self._evaluate_alert(alert, rt, now)

        for alert in self.catalog.enabled_live_alerts():
            if alert.id == "presenca_portao_parado":
                continue
            if alert.entity_id != entity_id:
                continue
            rt = self._runtime(alert.id)
            if not state_matches(new_state, alert.when_state):
                if rt.last_matched:
                    log.info(
                        "Alerta %s: estado %s (condicao %s) — reset cooldown",
                        alert.id,
                        new_state,
                        alert.when_state,
                    )
                rt.last_matched = False
                continue
            if state_matches(old_state, alert.when_state):
                continue
            if alert.id == "barreira_ir_disparo":
                asyncio.create_task(
                    self._check_barreira_ir_delayed(alert, rt, new_state),
                    name="shakira-barreira-ir-delayed",
                )
                continue

            await self._evaluate_alert(
                alert,
                rt,
                now,
                state_override=new_state,
            )

        if self._alarm_dispatch_active() and entity_id in self._alarm_dispatch.partition_entity_ids:  # type: ignore[union-attr]
            await self._alarm_dispatch.handle_live_state_change(  # type: ignore[union-attr]
                entity_id,
                old_state,
                new_state,
                _event_data,
            )

        if self._rain_dispatch_active() and entity_id in self._rain_dispatch.watched_entity_ids:  # type: ignore[union-attr]
            await self._rain_dispatch.handle_live_state_change(  # type: ignore[union-attr]
                entity_id,
                old_state,
                new_state,
                _event_data,
            )

        if self._interfone_dispatch_active() and entity_id in self._interfone_dispatch.watched_entity_ids:  # type: ignore[union-attr]
            await self._interfone_dispatch.handle_live_state_change(  # type: ignore[union-attr]
                entity_id,
                old_state,
                new_state,
                _event_data,
            )

        if self._presence_simulator_active() and entity_id == self._presence_simulator.config.control_entity:  # type: ignore[union-attr]
            await self._presence_simulator.handle_live_state_change(  # type: ignore[union-attr]
                entity_id,
                old_state,
                new_state,
                _event_data,
            )

        if entity_id in (
            "switch.bomba_fumaca_garagem_aquecimento",
            "switch.bomba_fumaca_sala_aquecimento",
            "switch.bomba_fumaca_despenca_aquecimento",
        ):
            new_norm = (new_state or "").strip().lower()
            old_norm = (old_state or "").strip().lower() if old_state else ""
            if new_norm == "on" and old_norm != "on":
                asyncio.create_task(
                    self._notify_smoke_bomb_heating_change(entity_id, is_on=True),
                    name="shakira-smoke-heating-notification",
                )
            elif new_norm == "off" and old_norm == "on":
                # Se foi desligado devido ao timeout de 15 min, evita duplicar o aviso
                if entity_id in getattr(self, "_smoke_bombs_timeout_triggered", set()):
                    self._smoke_bombs_timeout_triggered.remove(entity_id)
                else:
                    asyncio.create_task(
                        self._notify_smoke_bomb_heating_change(entity_id, is_on=False),
                        name="shakira-smoke-heating-notification",
                    )

    async def _notify_smoke_bomb_heating_change(self, entity_id: str, is_on: bool) -> None:
        labels = {
            "switch.bomba_fumaca_garagem_aquecimento": "Garagem",
            "switch.bomba_fumaca_sala_aquecimento": "Sala",
            "switch.bomba_fumaca_despenca_aquecimento": "Despensa",
        }
        room = labels.get(entity_id, entity_id)
        state_str = "LIGADO" if is_on else "DESLIGADO"
        emoji = "⚠️" if is_on else "ℹ️"
        message = f"{emoji} *AVISO:* O aquecimento da bomba de fumaça da *{room}* foi *{state_str}*!"
        
        phones = await resolve_notify_phones(
            self.ha,
            phones=[],
            default_phones=self.catalog.default_notify.phones,
        )
        if not phones:
            log.warning("Bomba fumaça: nenhum telefone configurado para notificação")
            return
            
        for phone in phones:
            try:
                await send_whatsapp_text(
                    settings=self.settings,
                    evo=self.evo,
                    number=phone,
                    message=message,
                )
            except Exception as e:
                log.warning("Bomba fumaça: falha ao enviar notificacao para %s: %s", phone, e)

    async def _check_barreira_ir_delayed(
        self,
        alert: AlertConfig,
        rt: _AlertRuntime,
        state_override: str,
    ) -> None:
        """Atrasa o disparo da barreira IR em 1.5s para descartar falsos positivos rápidos."""
        await asyncio.sleep(1.5)
        try:
            state_data = await self.ha.get_state(alert.entity_id)
            if not state_data:
                return
            current_state = str(state_data.get("state", "")).strip().lower()
            if state_matches(current_state, alert.when_state):
                log.info("Barreira IR: Sensor permaneceu ativo por > 1.5s. Confirmando disparo.")
                now = time.monotonic()
                await self._evaluate_alert(
                    alert,
                    rt,
                    now,
                    state_override=current_state,
                )
            else:
                log.info("Barreira IR: Sensor normalizado em menos de 1.5s. Disparo ignorado.")
        except Exception as e:
            log.warning("Erro no atraso da barreira IR: %s", e)

    async def _evaluate_alert(
        self,
        alert: AlertConfig,
        rt: _AlertRuntime,
        now: float,
        *,
        state_override: str | None = None,
    ) -> None:
        if alert.id == "presenca_portao_parado":
            presence_state_data = await self.ha.get_state("binary_sensor.presenca_porta_vidro_presence")
            distance_state_data = await self.ha.get_state("sensor.presenca_porta_vidro_target_distance")
            
            presence = presence_state_data.get("state") if presence_state_data else "off"
            distance_str = distance_state_data.get("state") if distance_state_data else None
            
            distance_ok = False
            if distance_str is not None:
                try:
                    distance_val = float(str(distance_str).replace(",", "."))
                    distance_ok = distance_val <= 2.0
                except ValueError:
                    pass
            
            is_present_close = (presence == "on" and distance_ok)
            
            if is_present_close:
                if not hasattr(rt, "first_matched_at") or rt.first_matched_at is None:
                    rt.first_matched_at = now  # type: ignore[attr-defined]
                    log.info("Alerta presenca_portao_parado: presenca e proximidade detectadas. Iniciando temporizador.")
                
                elapsed = now - rt.first_matched_at  # type: ignore[attr-defined]
                matched = elapsed >= 120.0
            else:
                if hasattr(rt, "first_matched_at") and rt.first_matched_at is not None:
                    log.info("Alerta presenca_portao_parado: alvo ausente ou afastou-se. Resetando temporizador.")
                rt.first_matched_at = None  # type: ignore[attr-defined]
                matched = False
                
            state = f"{presence} (distancia: {distance_str}m)"
        else:
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
                "Alerta %s: nenhum destino (configure default_notify/notify.phones ou %s)",
                alert.id,
                permitted_entity_hint(),
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
                    await self._send_alert_camera_snapshots(
                        alert, phone, camera_ids, context=text
                    )
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
                    "describe_cameras": alert.describe_cameras,
                    "describe_cameras_watch": alert.describe_cameras_watch or None,
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

    async def _evaluate_vibration_alert(self, sensor_id: str) -> None:
        """Verifica se as condições de disparo de sirene por vibração foram atendidas."""
        rule = next((r for r in VIBRATION_RULES if r["sensor"] == sensor_id), None)
        if not rule:
            return

        partition_id = rule["partition"]
        config_id = rule["config"]
        siren_id = rule["siren"]
        volume_id = rule["volume"]
        sensor_name = rule["name"]

        # 1. Verifica estado da partição associada
        partition_state_data = await self.ha.get_state(partition_id)
        partition_state = str(partition_state_data.get("state", "")).strip().lower() if partition_state_data else "disarmed"
        is_armed = partition_state.startswith("armed") or partition_state in ("triggered", "arming", "pending")
        if not is_armed:
            log.info("AlertsRunner: Vibração detectada em %s mas a partição %s está desarmada (%s)", sensor_name, partition_id, partition_state)
            return

        # 2. Verifica estado do boolean de configuração (se aplicável)
        if config_id:
            config_state_data = await self.ha.get_state(config_id)
            config_state = str(config_state_data.get("state", "")).strip().lower() if config_state_data else "off"
            if config_state != "on":
                log.info("AlertsRunner: Vibração detectada em %s com partição armada, mas monitoramento desativado via %s", sensor_name, config_id)
                return

        # 3. Dispara a sirene por 1 minuto
        log.warning("AlertsRunner: DISPARO DE SIRENE por vibração detectada em %s! Siren=%s", sensor_name, siren_id)

        if not hasattr(self, "_active_siren_tasks"):
            self._active_siren_tasks = {}

        existing_task = self._active_siren_tasks.get(siren_id)
        if existing_task and not existing_task.done():
            log.info("AlertsRunner: Reiniciando timer de 1 minuto para a sirene %s devido a nova vibração em %s.", siren_id, sensor_name)
            existing_task.cancel()

        task = asyncio.create_task(
            self._run_siren_timer(siren_id, volume_id, sensor_name),
            name=f"shakira-siren-timer-{siren_id}"
        )
        self._active_siren_tasks[siren_id] = task

    async def _run_siren_timer(self, siren_id: str, volume_id: str, sensor_name: str) -> None:
        """Executa disparo de sirene por 1 minuto a 30% e notifica moradores."""
        try:
            # 1. Envia notificação imediata
            notify_phones = await resolve_notify_phones(
                self.ha,
                phones=[],
                default_phones=self.catalog.default_notify.phones,
            )
            msg = (
                f"🚨 *ALERTA DE SEGURANÇA: VIBRAÇÃO DETECTADA!*\n\n"
                f"Foi detectada uma vibração no sensor *{sensor_name}* com o perímetro de alarme armado.\n"
                f"Acionando a sirene associada por 1 minuto a 30% de volume para dissuasão."
            )
            for phone in notify_phones:
                try:
                    await send_whatsapp_text(
                        settings=self.settings,
                        evo=self.evo,
                        number=phone,
                        message=msg,
                    )
                except Exception as e:
                    log.error("AlertsRunner: Falha ao notificar telefone %s de vibracao: %s", phone, e)

            # 2. Ajusta o volume para 30%
            try:
                await self.ha.call_service(
                    domain="number",
                    service="set_value",
                    service_data={"entity_id": volume_id, "value": 30.0}
                )
            except Exception as e:
                log.error("AlertsRunner: Falha ao definir volume da sirene %s: %s", volume_id, e)

            # 3. Liga a sirene
            try:
                await self.ha.call_service(
                    domain="switch",
                    service="turn_on",
                    service_data={"entity_id": siren_id}
                )
            except Exception as e:
                log.error("AlertsRunner: Falha ao ligar sirene %s: %s", siren_id, e)

            # 4. Aguarda 1 minuto (60 segundos)
            await asyncio.sleep(60.0)

            # 5. Desliga a sirene
            log.info("AlertsRunner: Fim do timer de 1 minuto. Desligando a sirene %s.", siren_id)
            try:
                await self.ha.call_service(
                    domain="switch",
                    service="turn_off",
                    service_data={"entity_id": siren_id}
                )
            except Exception as e:
                log.error("AlertsRunner: Falha ao desligar sirene %s: %s", siren_id, e)

            # 6. Notifica moradores sobre desligamento da sirene
            msg_off = (
                f"ℹ️ *SIRENE DESLIGADA*\n\n"
                f"A sirene associada ao sensor *{sensor_name}* foi desligada automaticamente após 1 minuto de funcionamento."
            )
            for phone in notify_phones:
                try:
                    await send_whatsapp_text(
                        settings=self.settings,
                        evo=self.evo,
                        number=phone,
                        message=msg_off,
                    )
                except Exception:
                    pass

        except asyncio.CancelledError:
            log.info("AlertsRunner: Timer da sirene %s cancelado devido a novo sinal de vibração ou parada.", siren_id)
        except Exception as e:
            log.error("AlertsRunner: Erro na execucao do timer da sirene %s: %s", siren_id, e)
