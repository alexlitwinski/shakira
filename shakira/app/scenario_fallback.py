"""Fallback quando o Gemini devolve resposta incompleta em cenarios do catalogo."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.devices_catalog import DevicesCatalog, ScenarioConfig
from app.homeassistant import HomeAssistantClient
from app.user_friendly import entity_display_name, format_ha_error_user

log = logging.getLogger(__name__)

ENTITY_ID_RE = re.compile(
    r"\b(?:sensor|switch|input_select|lock|light|binary_sensor|number|climate|cover|fan)\."
    r"[a-z0-9_]+\b",
    re.IGNORECASE,
)

BATH_USER_RE = re.compile(
    r"\b(banho|banhar|água|agua|boiler|quente|temperatura)\b",
    re.IGNORECASE,
)

YES_RE = re.compile(
    r"^\s*(sim|s|yes|pode|liga|ligue|aquece|aqueça|quero|confirmo|ok)\b",
    re.IGNORECASE,
)

HEAT_ASKED_RE = re.compile(r"\b(aquec|ligar o boiler|ligue o boiler)\b", re.IGNORECASE)


def message_suggests_bath_scenario(user_text: str) -> bool:
    return bool(BATH_USER_RE.search(user_text or ""))


def _match_scenario(user_text: str, catalog: DevicesCatalog, scenario_id: str | None) -> ScenarioConfig | None:
    if scenario_id:
        for sc in catalog.scenarios:
            if sc.id == scenario_id:
                return sc
    for sc in catalog.scenarios:
        if sc.id == "banho_boiler" and message_suggests_bath_scenario(user_text):
            return sc
    for sc in catalog.scenarios:
        if message_suggests_bath_scenario(user_text) and "boiler" in sc.prompt.lower():
            return sc
    return None


def _entities_from_prompt(prompt: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in ENTITY_ID_RE.finditer(prompt):
        eid = m.group(0).lower()
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def _temp_threshold_c(prompt: str) -> float:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*graus", prompt, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))
    return 42.0


def _state_float(state: dict[str, Any] | None) -> float | None:
    if not state:
        return None
    raw = state.get("state")
    if raw in (None, "unknown", "unavailable", ""):
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def _user_confirmed_heat(user_text: str, history_text: str) -> bool:
    if not YES_RE.search((user_text or "").strip()):
        return False
    return bool(HEAT_ASKED_RE.search(history_text or ""))


OnStep = Callable[[str], Awaitable[None]]


async def _emit(text: str, on_step: OnStep | None) -> bool:
    if on_step:
        await on_step(text)
        return True
    return False


async def try_scenario_fallback_reply(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    user_text: str,
    history_text: str = "",
    scenario_id: str | None = None,
    on_step: OnStep | None = None,
) -> str | None:
    """Completa cenarios de banho/boiler consultando o HA diretamente."""
    scenario = _match_scenario(user_text, catalog, scenario_id)
    if not scenario:
        return None

    entities = _entities_from_prompt(scenario.prompt)
    sensors = [e for e in entities if e.startswith("sensor.")]
    selects = [e for e in entities if e.startswith("input_select.")]

    if not sensors:
        log.warning("Cenario %s sem sensor no prompt; fallback ignorado", scenario.id)
        return None

    await _emit("Vou verificar a agua do boiler para voce...", on_step)

    sensor_id = sensors[0]
    threshold = _temp_threshold_c(scenario.prompt)

    if _user_confirmed_heat(user_text, history_text):
        select_id = selects[0] if selects else ""
        if not select_id:
            msg = "Nao encontrei o controle do boiler na configuracao."
            await _emit(msg, on_step)
            return None if on_step else msg
        if select_id not in catalog.actionable_entity_ids():
            msg = "Nao tenho permissao para ligar o boiler por aqui."
            await _emit(msg, on_step)
            return None if on_step else msg
        await _emit("Vou ligar o aquecimento do boiler...", on_step)
        try:
            await ha.call_service(
                "input_select",
                "select_option",
                {"entity_id": select_id, "option": "Ligado"},
            )
            log.info("Fallback cenario %s: %s -> Ligado", scenario.id, select_id)
            msg = "Liguei o boiler para aquecer a agua. Avise quando quiser verificar de novo."
            await _emit(msg, on_step)
            return None if on_step else msg
        except Exception as e:
            log.warning("Fallback call_service falhou: %s", e)
            msg = format_ha_error_user()
            await _emit(msg, on_step)
            return None if on_step else msg

    await _emit("Vou medir a temperatura da agua...", on_step)
    st = await ha.get_state(sensor_id)
    temp = _state_float(st)

    if temp is None:
        label = entity_display_name(sensor_id, catalog, st)
        msg = f"Nao consegui ler a temperatura de {label} agora."
        await _emit(msg, on_step)
        return None if on_step else msg

    temp_r = round(temp, 1)
    thr_r = int(threshold) if threshold == int(threshold) else threshold

    if temp >= threshold:
        msg = (
            f"A agua do boiler esta a {temp_r}°C (minimo recomendado: {thr_r}°C). "
            f"Pode tomar banho."
        )
    else:
        msg = (
            f"A agua esta a {temp_r}°C — ainda fria para um banho confortavel "
            f"(ideal: {thr_r}°C ou mais). Quer que eu ligue o boiler para aquecer?"
        )
    await _emit(msg, on_step)
    return None if on_step else msg
