"""Rotina de simulação de presença de pessoas na casa (aciona aleatoriamente luzes marcadas)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.alerts_catalog import PresenceSimulatorConfig
from app.config import AppSettings
from app.devices_catalog import DevicesCatalog
from app.homeassistant import HomeAssistantClient

log = logging.getLogger(__name__)@dataclass
class PresenceSimulatorRunner:
    settings: AppSettings
    ha: HomeAssistantClient
    config: PresenceSimulatorConfig
    devices: DevicesCatalog | None = None
    _loop_task: asyncio.Task[None] | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _active_light: str | None = None
    _active_until: float | None = None
    _next_action_at: float | None = None
    _bootstrapped: bool = False
    _history: list[dict[str, Any]] = field(default_factory=list)
    _last_control_state: bool | None = None

    def _log_action(self, action_type: str, entity_id: str | None, details: str) -> None:
        from datetime import datetime
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self._history.append({
            "timestamp": now_str,
            "action": action_type,
            "entity_id": entity_id,
            "details": details
        })
        if len(self._history) > 15:
            self._history.pop(0)

    def reload(self, config: PresenceSimulatorConfig, *, devices: DevicesCatalog | None = None) -> None:
        self.config = config
        if devices is not None:
            self.devices = devices
        log.info("Rotina de simulação de presença recarregada.")
        self._log_action("Recarregar", None, "Configuração do simulador recarregada.")

    async def ensure_running(self) -> None:
        if self.config.enabled:
            self.start()
        else:
            await self.stop()

    def start(self) -> None:
        if not self.config.enabled:
            log.info("Rotina de simulação de presença desativada (presence_simulator.enabled=false)")
            return
        if self._loop_task and not self._loop_task.done():
            return
        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="shakira-presence-simulator")
        log.info(
            "Rotina de simulação de presença iniciada (control_entity=%s, ON: %.1f-%.1f min, OFF: %.1f-%.1f min)",
            self.config.control_entity,
            self.config.min_on_minutes,
            self.config.max_on_minutes,
            self.config.min_off_minutes,
            self.config.max_off_minutes,
        )
        self._log_action("Iniciar", None, "Serviço de simulação de presença iniciado em segundo plano.")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        await self._turn_off_active_light("simulador parado")
        self._next_action_at = None
        self._log_action("Parar", None, "Serviço de simulação de presença parado.")

    def is_running(self) -> bool:
        return bool(self.config.enabled and self._loop_task and not self._loop_task.done())

    async def handle_live_state_change(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str,
        _event_data: dict[str, Any],
    ) -> None:
        if entity_id != self.config.control_entity:
            return

        state_norm = str(new_state).strip().lower()
        log.info("Simulação de presença (live): %s alterou %s -> %s", entity_id, old_state or "(vazio)", new_state)

        if state_norm == "off":
            await self._turn_off_active_light("botão de controle desligado (live)")
            self._next_action_at = None

    def _get_eligible_entities(self) -> list[str]:
        if not self.devices:
            return []
        eligible = []
        for dev in self.devices.devices:
            for ent in dev.entities:
                if ent.allow_actions and ent.simular_presenca:
                    eligible.append(ent.entity_id)
        return eligible

    async def _turn_off_active_light(self, reason: str = "expiração de tempo") -> None:
        if self._active_light:
            try:
                domain = self._active_light.split(".")[0]
                await self.ha.call_service(domain, "turn_off", {"entity_id": self._active_light})
                log.info("Simulação de presença: %s desligado com sucesso. Razão: %s", self._active_light, reason)
                self._log_action("Desligar", self._active_light, f"Desligou {self._active_light} (razão: {reason}).")
            except Exception as e:
                log.error("Simulação de presença: falha ao desligar %s: %s", self._active_light, e)
                self._log_action("Erro", self._active_light, f"Falha ao desligar {self._active_light}: {e}")
            finally:
                self._active_light = None
                self._active_until = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # 1. Verifica estado atual do control_entity
                control_state_data = await self.ha.get_state(self.config.control_entity)
                is_active = control_state_data.get("state") == "on" if control_state_data else False

                if self._last_control_state is None:
                    self._last_control_state = is_active
                    state_desc = "Ativo" if is_active else "Inativo"
                    self._log_action("Inicializado", None, f"Simulador de presença inicializado como {state_desc}.")
                elif self._last_control_state != is_active:
                    state_desc = "Ativado" if is_active else "Desativado"
                    self._log_action("Mudança de Estado", None, f"O simulador foi {state_desc} pelo Home Assistant.")
                    self._last_control_state = is_active

                if not is_active:
                    # Simulação inativa. Garante que tudo esteja desligado.
                    await self._turn_off_active_light("botão de controle desligado")
                    self._next_action_at = None
                    await asyncio.sleep(10)
                    continue

                now = time.time()

                # 2. Se há uma luz ligada pela simulação, verifica se deve desligar
                if self._active_light and self._active_until:
                    if now >= self._active_until:
                        log.info("Simulação de presença: tempo ativo expirado para %s. Desligando.", self._active_light)
                        await self._turn_off_active_light("fim do tempo ativo")
                        
                        # Calcula próximo tempo de acionamento (OFF)
                        off_sec = random.uniform(self.config.min_off_minutes * 60, self.config.max_off_minutes * 60)
                        self._next_action_at = now + off_sec
                        log.info(
                            "Simulação de presença: próxima luz acionará em %.1f minutos (às %s)",
                            off_sec / 60.0,
                            time.strftime("%H:%M:%S", time.localtime(self._next_action_at)),
                        )
                        self._log_action("Agendar", None, f"Aguardando {off_sec / 60.0:.1f} minutos para a próxima luz.")
                    else:
                        await asyncio.sleep(5)
                        continue

                # 3. Se nenhuma luz está ligada, verifica se é hora de ligar uma
                if self._next_action_at is None:
                    # Inicialização ou transição imediata
                    self._next_action_at = now

                if now >= self._next_action_at:
                    eligible = self._get_eligible_entities()
                    if not eligible:
                        log.warning("Simulação de presença ativa, mas nenhuma entidade com allow_actions=true e simular_presenca=true encontrada no catálogo.")
                        await asyncio.sleep(60)
                        continue

                    selected = random.choice(eligible)
                    on_sec = random.uniform(self.config.min_on_minutes * 60, self.config.max_on_minutes * 60)
                    
                    log.info("Simulação de presença: acionando aleatoriamente %s por %.1f minutos.", selected, on_sec / 60.0)
                    try:
                        domain = selected.split(".")[0]
                        await self.ha.call_service(domain, "turn_on", {"entity_id": selected})
                        self._active_light = selected
                        self._active_until = now + on_sec
                        self._next_action_at = None
                        self._log_action("Ligar", selected, f"Acionou {selected} por {on_sec / 60.0:.1f} minutos.")
                    except Exception as e:
                        log.error("Simulação de presença: falha ao acionar %s: %s", selected, e)
                        self._log_action("Erro", selected, f"Falha ao ligar {selected}: {e}")
                        self._next_action_at = now + 30  # Tenta novamente em 30 segundos
            except Exception:
                log.exception("Erro no ciclo da simulação de presença")

            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break

    def status_snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": self.config.enabled,
            "running": self.is_running(),
            "control_entity": self.config.control_entity,
            "min_on_minutes": self.config.min_on_minutes,
            "max_on_minutes": self.config.max_on_minutes,
            "min_off_minutes": self.config.min_off_minutes,
            "max_off_minutes": self.config.max_off_minutes,
            "active_light": self._active_light,
            "active_until_in_s": round(self._active_until - now, 1) if self._active_until else None,
            "next_action_in_s": round(self._next_action_at - now, 1) if self._next_action_at else None,
            "history": self._history,
        }
