"""Rotina de avisos de chuva (sensor, volume 15 min, porta de vidro e toldo gourmet)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.alerts_catalog import RainDispatchConfig
from app.config import AppSettings
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.homeassistant import HomeAssistantClient
from app.whatsapp_phones import (
    fetch_permitted_phones_raw,
    normalize_phone_digits,
    parse_allowed_numbers,
)
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

DEFAULT_PORTA_VIDRO_LABEL = "Porta de vidro da cozinha gourmet"
DEFAULT_TOLDO_LABEL = "Toldo da área gourmet"

POLL_TICK_SECONDS = 60
STATE_ON = "on"
COVER_OPEN = "open"
COVER_CLOSED = "closed"
WINDOW_OPEN_STATES = frozenset({"open", "on"})


def parse_rain_volume(state: str) -> float:
    raw = (state or "").strip().replace(",", ".")
    if not raw or raw.lower() in ("unknown", "unavailable", "none"):
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def is_raining(state: str) -> bool:
    return (state or "").strip().lower() == STATE_ON


def cover_is_open(state: str) -> bool:
    return (state or "").strip().lower() == COVER_OPEN


def cover_is_closed(state: str) -> bool:
    return (state or "").strip().lower() == COVER_CLOSED


def window_is_open(state: str) -> bool:
    return (state or "").strip().lower() in WINDOW_OPEN_STATES


def rain_started_transition(
    *,
    rain_entity: str,
    entity_id: str | None,
    old_state: str | None,
    new_state: str | None,
    prev_rain_on: bool | None,
    rain_on: bool,
    source: str,
    bootstrapped: bool,
) -> bool:
    """Detecta inicio de chuva (live com old/new ou poll com prev vs atual)."""
    if entity_id == rain_entity and new_state is not None:
        if old_state is not None:
            return not is_raining(old_state) and is_raining(new_state)
        if source == "live" and not bootstrapped and is_raining(new_state):
            return True
    if prev_rain_on is None:
        return False
    return not prev_rain_on and rain_on


@dataclass(frozen=True)
class RainStartStatus:
    open_windows: list[str]
    porta_vidro_open: bool
    toldo_closed: bool
    porta_label: str = DEFAULT_PORTA_VIDRO_LABEL
    toldo_label: str = DEFAULT_TOLDO_LABEL


def build_rain_started_message(status: RainStartStatus) -> str:
    """Resumo ao iniciar chuva: alertas e confirmacao quando tudo estiver ok."""
    lines = ["Começou a chover.", "", "Situação:"]

    if status.open_windows:
        lines.append("• Janelas abertas:")
        for label in status.open_windows:
            lines.append(f"  - {label}")
        lines.append("  Feche as janelas.")
    else:
        lines.append("• Janelas: nenhuma aberta.")

    if status.toldo_closed:
        lines.append(
            f"• {status.toldo_label}: fechado — abra o toldo para proteger o espaço."
        )
    else:
        lines.append(f"• {status.toldo_label}: aberto (recolhido).")

    if status.porta_vidro_open:
        lines.append(
            f"• {status.porta_label}: aberta — considere fechar."
        )
    else:
        lines.append(f"• {status.porta_label}: fechada.")

    return "\n".join(lines)


@dataclass
class RainDispatchRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    evo: EvolutionClient
    config: RainDispatchConfig
    devices: DevicesCatalog | None = None
    _poll_task: asyncio.Task[None] | None = None
    _poll_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_rain_on: bool | None = None
    _last_volume_mm: float | None = None
    _last_cover_states: dict[str, str] = field(default_factory=dict)
    _last_notify_at: dict[str, float] = field(default_factory=dict)
    _bootstrapped: bool = False

    @property
    def watched_entity_ids(self) -> set[str]:
        return {
            self.config.rain_entity,
            self.config.volume_entity,
            self.config.porta_vidro_entity,
            self.config.toldo_entity,
        }

    def reload(self, config: RainDispatchConfig, *, devices: DevicesCatalog | None = None) -> None:
        self.config = config
        if devices is not None:
            self.devices = devices

    async def ensure_running(self) -> None:
        if self.config.enabled:
            self._start_poll_loop()
            if not self._bootstrapped:
                asyncio.create_task(self._bootstrap_states(), name="shakira-rain-bootstrap")
        else:
            await self.stop()

    def start(self) -> None:
        if not self.config.enabled:
            log.info("Rotina de chuva desativada (rain_dispatch.enabled=false)")
            return
        self._start_poll_loop()
        if not self._bootstrapped:
            asyncio.create_task(self._bootstrap_states(), name="shakira-rain-bootstrap")
        log.info(
            "Rotina de chuva ativa (entidades=%s, chuva forte >= %.1f mm/15min)",
            sorted(self.watched_entity_ids),
            self.config.heavy_rain_mm,
        )

    def _start_poll_loop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="shakira-rain-dispatch-poll")

    async def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    def is_running(self) -> bool:
        return bool(self.config.enabled and self._poll_task and not self._poll_task.done())

    async def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                await self._evaluate_all(source="poll")
            except Exception:
                log.exception("Erro no poll da rotina de chuva")
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=POLL_TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        if entity_id not in self.watched_entity_ids:
            return
        if entity_id == self.config.rain_entity:
            log.info(
                "Chuva (live): %s %s -> %s",
                entity_id,
                old_state or "(vazio)",
                new_state,
            )
        await self._evaluate_all(source="live", entity_id=entity_id, old_state=old_state, new_state=new_state)

    async def _evaluate_all(
        self,
        *,
        source: str,
        entity_id: str | None = None,
        old_state: str | None = None,
        new_state: str | None = None,
    ) -> None:
        bootstrapped_before = self._bootstrapped
        if not self._bootstrapped:
            await self._bootstrap_states()
            if source == "poll":
                return

        rain_state = await self._get_state(self.config.rain_entity)
        volume_state = await self._get_state(self.config.volume_entity)
        porta_state = await self._get_state(self.config.porta_vidro_entity)
        toldo_state = await self._get_state(self.config.toldo_entity)

        rain_on = is_raining(rain_state)
        volume_mm = parse_rain_volume(volume_state)
        heavy = volume_mm >= self.config.heavy_rain_mm

        prev_rain = self._last_rain_on
        prev_volume = self._last_volume_mm if self._last_volume_mm is not None else 0.0

        if entity_id == self.config.rain_entity and new_state is not None:
            rain_on = is_raining(new_state)
        if entity_id == self.config.volume_entity and new_state is not None:
            volume_mm = parse_rain_volume(new_state)
            heavy = volume_mm >= self.config.heavy_rain_mm

        started = rain_started_transition(
            rain_entity=self.config.rain_entity,
            entity_id=entity_id,
            old_state=old_state,
            new_state=new_state,
            prev_rain_on=prev_rain,
            rain_on=rain_on,
            source=source,
            bootstrapped=bootstrapped_before,
        )
        if started:
            status = await self._gather_rain_start_status(porta_state, toldo_state)
            log.info(
                "Chuva: inicio detectado (source=%s, janelas=%s, toldo_fechado=%s, porta_aberta=%s)",
                source,
                status.open_windows,
                status.toldo_closed,
                status.porta_vidro_open,
            )
            await self._notify(
                "rain_started",
                build_rain_started_message(status),
            )

        if prev_volume > 0 and volume_mm == 0:
            await self._notify(
                "rain_stopped",
                "A chuva parou (volume nos últimos 15 minutos em 0 mm).",
            )

        if heavy and prev_volume < self.config.heavy_rain_mm:
            await self._notify(
                "rain_heavy",
                f"Volume de chuva elevado: {volume_mm:g} mm nos últimos 15 minutos.",
            )

        if heavy and cover_is_open(porta_state):
            await self._notify(
                "porta_open_heavy",
                f"Chuva forte ({volume_mm:g} mm/15 min) e a porta de vidro da cozinha gourmet "
                "está aberta. Considere fechá-la.",
            )

        if (
            entity_id == self.config.porta_vidro_entity
            and new_state
            and cover_is_open(new_state)
            and heavy
            and not cover_is_open(self._last_cover_states.get(self.config.porta_vidro_entity, ""))
        ):
            await self._notify(
                "porta_open_heavy",
                f"Chuva forte ({volume_mm:g} mm/15 min) e a porta de vidro da cozinha gourmet "
                "está aberta. Considere fechá-la.",
            )

        self._last_rain_on = rain_on
        self._last_volume_mm = volume_mm
        self._last_cover_states[self.config.porta_vidro_entity] = porta_state
        self._last_cover_states[self.config.toldo_entity] = toldo_state

    async def _bootstrap_states(self) -> None:
        self._last_rain_on = is_raining(await self._get_state(self.config.rain_entity))
        self._last_volume_mm = parse_rain_volume(await self._get_state(self.config.volume_entity))
        self._last_cover_states[self.config.porta_vidro_entity] = await self._get_state(
            self.config.porta_vidro_entity
        )
        self._last_cover_states[self.config.toldo_entity] = await self._get_state(
            self.config.toldo_entity
        )
        self._bootstrapped = True
        log.info(
            "Rotina de chuva: estado inicial chuva=%s volume=%.1f mm",
            self._last_rain_on,
            self._last_volume_mm,
        )

    def _entity_label(self, entity_id: str, default: str) -> str:
        if self.devices:
            ent = self.devices.get_entity(entity_id)
            if ent and ent.description.strip():
                return ent.description.strip()
        return default

    async def _fetch_open_window_labels(self) -> list[str]:
        if not self.devices:
            return []
        open_labels: list[str] = []
        for entity_id, label in self.devices.entities_by_opening_kind("window"):
            if window_is_open(await self._get_state(entity_id)):
                open_labels.append(label)
        return open_labels

    async def _gather_rain_start_status(
        self,
        porta_state: str,
        toldo_state: str,
    ) -> RainStartStatus:
        return RainStartStatus(
            open_windows=await self._fetch_open_window_labels(),
            porta_vidro_open=cover_is_open(porta_state),
            toldo_closed=cover_is_closed(toldo_state),
            porta_label=self._entity_label(
                self.config.porta_vidro_entity,
                DEFAULT_PORTA_VIDRO_LABEL,
            ),
            toldo_label=self._entity_label(
                self.config.toldo_entity,
                DEFAULT_TOLDO_LABEL,
            ),
        )

    async def _get_state(self, entity_id: str) -> str:
        data = await self.ha.get_state(entity_id)
        if not data:
            return ""
        return str(data.get("state", "")).strip()

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

    def _cooldown_ok(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last_notify_at.get(key, 0.0)
        return now - last >= self.config.cooldown_seconds

    async def _notify(self, cooldown_key: str, message: str) -> None:
        if not self._cooldown_ok(cooldown_key):
            log.info("Chuva: aviso %s ignorado (cooldown)", cooldown_key)
            return
        phones = await self._resolve_phones()
        if not phones:
            log.warning("Chuva: nenhum destino WhatsApp configurado")
            return
        sent = False
        for phone in phones:
            try:
                await send_whatsapp_text(
                    settings=self.settings,
                    evo=self.evo,
                    number=phone,
                    message=message,
                )
                sent = True
            except WhatsAppSendError as e:
                log.warning("Chuva: falha WhatsApp phone=%s: %s", phone, e)
        if sent:
            self._last_notify_at[cooldown_key] = time.monotonic()
            log.info("Chuva: aviso enviado key=%s", cooldown_key)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "running": self.is_running(),
            "heavy_rain_mm": self.config.heavy_rain_mm,
            "cooldown_seconds": self.config.cooldown_seconds,
            "entities": {
                "rain": self.config.rain_entity,
                "volume_15m": self.config.volume_entity,
                "porta_vidro": self.config.porta_vidro_entity,
                "toldo": self.config.toldo_entity,
            },
            "last_rain_on": self._last_rain_on,
            "last_volume_mm": self._last_volume_mm,
            "last_cover_states": dict(self._last_cover_states),
            "websocket": {
                "mode": "shared_with_alerts_runner",
                "subscribed_entities": sorted(self.watched_entity_ids),
            },
        }
