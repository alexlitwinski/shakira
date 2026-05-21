"""Rotina dedicada ao disparo do alarme AMT 8000 (particoes + cameras + Gemini)."""



from __future__ import annotations



import asyncio

import logging

import os

import time

from dataclasses import dataclass, field

from typing import Any



import httpx



from app.alerts_catalog import AlarmDispatchConfig

from app.camera_snapshots import send_camera_snapshots

from app.camera_vision import (

    DEFAULT_MAX_VISION_RETRIES,

    analyze_camera_mosaic,

    format_analysis_message,

)

from app.cameras_catalog import CamerasCatalog

from app.config import AppSettings

from app.devices_catalog import DevicesCatalog

from app.evolution import EvolutionClient

from app.ha_websocket import HaWebSocketListener

from app.homeassistant import HomeAssistantClient

from app.whatsapp_phones import (

    fetch_permitted_phones_raw,

    normalize_phone_digits,

    parse_allowed_numbers,

)

from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text



log = logging.getLogger(__name__)



TRIGGERED_STATE = "triggered"



# Particoes monitoradas (entity_id -> rotulo padrao se nao houver descricao no catalogo)

DEFAULT_PARTITIONS: tuple[tuple[str, str], ...] = (

    ("alarm_control_panel.amt_8000_partition_1", "Partição 1 — perímetro externo"),

    ("alarm_control_panel.amt_8000_partition_2", "Partição 2 — perímetro interno"),

    ("alarm_control_panel.amt_8000_partition_3", "Partição 3 — cozinha gourmet e quarto de hóspedes"),

    ("alarm_control_panel.amt_8000_partition_4", "Partição 4 — sensores internos"),

    ("alarm_control_panel.amt_8000_partition_5", "Partição 5 — segundo andar"),

)





def build_trigger_message(triggered_sectors: list[str]) -> str:

    """Mensagem WhatsApp com os setores (particoes) em disparo."""

    if not triggered_sectors:

        return ""

    lines = ["ALERTA: alarme disparou!", "", "Setores com disparo:"]

    for label in triggered_sectors:

        lines.append(f"• {label}")

    return "\n".join(lines)





def resolve_camera_ids_for_partitions(

    cameras: CamerasCatalog,

    partition_entity_ids: list[str],

) -> list[str]:

    """

    Cameras cujo grupo em shakira_cameras.yaml inclui o entity_id da particao

    (ex.: group: ..., alarm_control_panel.amt_8000_partition_4).

    """

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

    devices: DevicesCatalog | None = None

    http: httpx.AsyncClient | None = None

    _ws_listener: HaWebSocketListener | None = None

    _debounce_task: asyncio.Task[None] | None = None

    _last_notified_at: float = 0.0

    _had_triggered: bool = False



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

    async def ensure_running(self) -> None:
        if self.config.enabled:
            self._ensure_websocket()
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

        configured = [

            normalize_phone_digits(p)

            for p in self.config.notify.phones

            if normalize_phone_digits(p)

        ]

        if configured:

            return configured

        raw = await fetch_permitted_phones_raw(self.ha)

        return sorted(parse_allowed_numbers(raw))



    async def _fetch_triggered_partitions(self) -> list[tuple[str, str]]:

        """Retorna [(entity_id, rotulo), ...] das particoes em triggered."""

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

        self._ensure_websocket()

        log.info(

            "Rotina de disparo do alarme iniciada (particoes=%s, debounce=%ss)",

            len(self.partition_entity_ids),

            self.config.debounce_seconds,

        )



    def _ensure_websocket(self) -> None:

        if self._ws_listener is None:

            self._ws_listener = HaWebSocketListener(

                settings=self.settings,

                on_state_changed=self.handle_live_state_change,

                entity_ids=self.partition_entity_ids,

            )

        else:

            self._ws_listener.update_entity_ids(self.partition_entity_ids)



        if not (self._ws_listener._task and not self._ws_listener._task.done()):

            self._ws_listener.start()



    async def stop(self) -> None:

        if self._debounce_task and not self._debounce_task.done():

            self._debounce_task.cancel()

            try:

                await self._debounce_task

            except asyncio.CancelledError:

                pass

            self._debounce_task = None

        if self._ws_listener:

            await self._ws_listener.stop()

            self._ws_listener = None



    def is_running(self) -> bool:

        return bool(

            self.config.enabled

            and self._ws_listener

            and self._ws_listener._task

            and not self._ws_listener._task.done()

        )



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



        if new_norm != TRIGGERED_STATE:

            if self._had_triggered and new_norm != TRIGGERED_STATE:

                still = await self._fetch_triggered_partitions()

                if not still:

                    self._had_triggered = False

                    log.info("Alarme: todas as particoes sairam de triggered")

            return



        if old_norm == TRIGGERED_STATE:

            return



        log.info("Alarme: particao %s entrou em triggered — agendando aviso", entity_id)

        self._schedule_debounced_dispatch()



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



    async def _dispatch_notification(self) -> None:

        triggered = await self._fetch_triggered_partitions()

        if not triggered:

            return



        now = time.monotonic()

        if self._last_notified_at and now - self._last_notified_at < self.config.cooldown_seconds:

            log.info(

                "Alarme: disparo ignorado (cooldown %.0fs, particoes=%s)",

                self.config.cooldown_seconds,

                [eid for eid, _ in triggered],

            )

            return



        phones = await self._resolve_phones()

        if not phones:

            log.warning("Alarme: nenhum destino WhatsApp configurado")

            return



        sector_labels = [label for _, label in triggered]

        partition_ids = [eid for eid, _ in triggered]

        message = build_trigger_message(sector_labels)

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

                "Alarme: aviso enviado setores=%s cameras=%s destinos=%s",

                sector_labels,

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



        if not self.config.describe_cameras or not result.image_bytes or result.sent <= 0:

            return



        api_key = self.settings.gemini_api_key.strip()

        if not api_key:

            log.warning("Alarme: describe_cameras ativo mas gemini_api_key ausente")

            return



        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

        try:

            analysis = await asyncio.to_thread(

                analyze_camera_mosaic,

                api_key=api_key,

                image_bytes=result.image_bytes,

                camera_panels=result.image_panels,

                context=context,

                model=model,

            )

        except Exception:

            log.exception("Alarme: falha Gemini vision phone=%s", phone)

            return



        if analysis is None:

            log.warning("Alarme: Gemini vision vazio phone=%s", phone)

            return



        description = format_analysis_message(analysis)

        if not description:

            return



        try:

            await send_whatsapp_text(

                settings=self.settings,

                evo=self.evo,

                number=phone,

                message=description,

            )

            log.info("Alarme: analise Gemini enviada phone=%s chars=%s", phone, len(description))

        except WhatsAppSendError as e:

            log.warning("Alarme: falha WhatsApp analise phone=%s: %s", phone, e)



    def status_snapshot(self) -> dict[str, Any]:

        ws_status: dict[str, Any] = {

            "connected": False,

            "subscribed_entities": sorted(self.partition_entity_ids),

        }

        if self._ws_listener:

            ws_status = self._ws_listener.status_snapshot()



        now = time.monotonic()

        return {

            "enabled": self.config.enabled,

            "running": self.is_running(),

            "describe_cameras": self.config.describe_cameras,

            "cooldown_seconds": self.config.cooldown_seconds,

            "debounce_seconds": self.config.debounce_seconds,

            "last_notified_ago_s": round(now - self._last_notified_at, 1)

            if self._last_notified_at

            else None,

            "partitions": [eid for eid, _ in DEFAULT_PARTITIONS],

            "websocket": ws_status,

        }


