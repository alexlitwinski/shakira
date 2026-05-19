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

from app.scenario_context import (
    message_suggests_bath_scenario,
    message_suggests_server_health,
)

YES_RE = re.compile(
    r"^\s*(sim|s|yes|pode|liga|ligue|aquece|aqueça|quero|confirmo|ok)\b",
    re.IGNORECASE,
)

HEAT_ASKED_RE = re.compile(r"\b(aquec|ligar o boiler|ligue o boiler)\b", re.IGNORECASE)

OnStep = Callable[[str], Awaitable[None]]


def _match_scenario(
    user_text: str, catalog: DevicesCatalog, scenario_id: str | None
) -> ScenarioConfig | None:
    if scenario_id:
        for sc in catalog.scenarios:
            if sc.id == scenario_id:
                return sc
    for sc in catalog.scenarios:
        if sc.id == "saude_servidor" and message_suggests_server_health(user_text):
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


def _server_entity_ids(catalog: DevicesCatalog, scenario: ScenarioConfig | None) -> list[str]:
    for dev in catalog.devices:
        if "servidor" in dev.name.lower():
            return [e.entity_id for e in dev.entities]
    if scenario:
        return _entities_from_prompt(scenario.prompt)
    return []


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


async def _emit(text: str, on_step: OnStep | None) -> bool:
    if on_step:
        await on_step(text)
        return True
    return False


def _ping_label(entity_id: str, catalog: DevicesCatalog) -> str:
    if "roteador" in entity_id:
        return "Roteador principal"
    if "otavio" in entity_id:
        return "DVR (Otavio)"
    if "3dmaker" in entity_id:
        return "Servidor 3D Maker (Omada)"
    return entity_display_name(entity_id, catalog)


def _format_server_line(
    entity_id: str, state: dict[str, Any] | None, catalog: DevicesCatalog
) -> tuple[str, str | None]:
    """Retorna (linha amigavel, aviso opcional)."""
    label = entity_display_name(entity_id, catalog, state)
    raw = str(state.get("state", "")) if state else ""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

    if raw in ("", "unknown", "unavailable") or state is None:
        return f"• {label}: indisponivel", f"{label} indisponivel"

    if domain == "binary_sensor" and "ping" in entity_id:
        ping_label = _ping_label(entity_id, catalog)
        if raw.lower() == "on":
            return f"• {ping_label}: online", None
        return f"• {ping_label}: offline", f"{ping_label} offline"

    if entity_id == "sensor.memory_use_percent":
        val = _state_float(state)
        if val is None:
            return f"• Memoria: {raw}", None
        issue = f"Memoria alta ({val:g}%)" if val >= 85 else None
        return f"• Memoria: {val:g}%", issue

    if entity_id == "sensor.processor_use":
        val = _state_float(state)
        if val is None:
            return f"• CPU: {raw}", None
        issue = f"CPU alta ({val:g}%)" if val >= 90 else None
        return f"• CPU: {val:g}%", issue

    if entity_id == "sensor.disk_use_percent_config":
        val = _state_float(state)
        if val is None:
            return f"• Disco (config): {raw}", None
        issue = f"Disco quase cheio ({val:g}%)" if val >= 90 else None
        return f"• Disco (config): {val:g}%", issue

    if entity_id == "sensor.armario_servidores_temperature":
        val = _state_float(state)
        if val is None:
            return f"• Temperatura do armario: {raw}", None
        issue = f"Armario quente ({val:g}°C)" if val > 35 else None
        return f"• Temperatura do armario: {val:g}°C", issue

    if entity_id == "sensor.processor_temperature":
        val = _state_float(state)
        if val is None:
            return f"• Temperatura do processador: {raw}", None
        issue = f"Processador quente ({val:g}°C)" if val > 80 else None
        return f"• Temperatura do processador: {val:g}°C", issue

    if domain == "climate":
        attrs = state.get("attributes") or {}
        current = attrs.get("current_temperature") if isinstance(attrs, dict) else None
        target = attrs.get("temperature") if isinstance(attrs, dict) else None
        extra = ""
        if current is not None:
            extra = f", agora {current}°C"
        if target is not None:
            extra += f", alvo {target}°C"
        return f"• {label}: {raw}{extra}", None

    if domain == "switch":
        on = raw.lower() in ("on", "true")
        return (
            (f"• {label}: ligada", None) if on else (f"• {label}: desligada", None)
        )

    return f"• {label}: {raw}", None


async def _run_server_health_fallback(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    on_step: OnStep | None,
) -> str | None:
    scenario = next((s for s in catalog.scenarios if s.id == "saude_servidor"), None)
    entity_ids = _server_entity_ids(catalog, scenario)
    if not entity_ids:
        log.warning("Fallback servidor: nenhuma entidade no catalogo")
        return None

    await _emit("Vou verificar o servidor e a rede...", on_step)

    lines: list[str] = []
    issues: list[str] = []
    for eid in entity_ids:
        st = await ha.get_state(eid)
        line, issue = _format_server_line(eid, st, catalog)
        lines.append(line)
        if issue:
            issues.append(issue)

    parts = ["Resumo do servidor:", ""] + lines
    if issues:
        parts.extend(["", "Atencao:"] + [f"• {i}" for i in issues])
    else:
        parts.extend(["", "Tudo parece normal por aqui."])

    msg = "\n".join(parts)
    await _emit(msg, on_step)
    return None if on_step else msg


async def _run_banho_boiler_fallback(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    scenario: ScenarioConfig,
    user_text: str,
    history_text: str,
    on_step: OnStep | None,
) -> str | None:
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


async def try_scenario_fallback_reply(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    user_text: str,
    history_text: str = "",
    scenario_id: str | None = None,
    on_step: OnStep | None = None,
) -> str | None:
    """Completa cenarios quando o Gemini nao conclui (banho, saude do servidor, etc.)."""
    scenario = _match_scenario(user_text, catalog, scenario_id)

    if scenario and scenario.id == "saude_servidor":
        return await _run_server_health_fallback(ha=ha, catalog=catalog, on_step=on_step)

    if (
        not scenario
        and message_suggests_server_health(user_text)
        and _server_entity_ids(catalog, None)
    ):
        return await _run_server_health_fallback(ha=ha, catalog=catalog, on_step=on_step)

    if scenario and (scenario.id == "banho_boiler" or "boiler" in scenario.prompt.lower()):
        return await _run_banho_boiler_fallback(
            ha=ha,
            catalog=catalog,
            scenario=scenario,
            user_text=user_text,
            history_text=history_text,
            on_step=on_step,
        )

    return None
