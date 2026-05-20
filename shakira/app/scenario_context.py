"""Utilitarios: entidades citadas nos prompts do YAML e leitura no Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.devices_catalog import DevicesCatalog, ScenarioConfig
from app.homeassistant import HomeAssistantClient

log = logging.getLogger(__name__)

ENTITY_ID_RE = re.compile(
    r"\b(?:sensor|switch|input_select|lock|light|binary_sensor|number|climate|cover|fan|"
    r"alarm_control_panel|scene)\.[a-z][a-z0-9_]+\b",
    re.IGNORECASE,
)

# Servicos HA citados no prompt (ex.: switch.turn_on) — nao sao entidades.
_INVALID_ENTITY_SUFFIXES = frozenset(
    {
        "turn_on",
        "turn_off",
        "toggle",
        "select_option",
        "open_cover",
        "close_cover",
    }
)


def entities_from_prompt(prompt: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in ENTITY_ID_RE.finditer(prompt or ""):
        eid = m.group(0).lower()
        suffix = eid.split(".", 1)[-1] if "." in eid else eid
        if suffix in _INVALID_ENTITY_SUFFIXES:
            continue
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def entity_ids_for_scenarios(scenarios: list[ScenarioConfig]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sc in scenarios:
        for eid in entities_from_prompt(sc.prompt):
            if eid not in seen:
                seen.add(eid)
                out.append(eid)
    return out


async def fetch_verified_entity_states(
    ha: HomeAssistantClient,
    entity_ids: list[str],
    *,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any] | None]:
    if not entity_ids:
        return {}

    if states_map is not None:
        return {eid: states_map.get(eid) for eid in entity_ids}

    async def _one(eid: str) -> tuple[str, dict[str, Any] | None]:
        return eid, await ha.get_state(eid)

    pairs = await asyncio.gather(*(_one(eid) for eid in entity_ids))
    out = dict(pairs)
    for eid, st in out.items():
        if st is None:
            log.warning("HA get_state sem resposta para %s", eid)
    return out


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


def format_entity_state_for_prompt(
    entity_id: str,
    state: dict[str, Any] | None,
    catalog: DevicesCatalog | None,
) -> str:
    ent = catalog.get_entity(entity_id) if catalog else None
    label = (
        ent.description.split("—")[0].split("-")[0].strip()
        if ent and ent.description
        else entity_id
    )
    if not state:
        return f"- {entity_id} ({label}): indisponivel"

    raw = str(state.get("state", ""))
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

    if domain == "sensor" and "temp" in entity_id.lower():
        val = _state_float(state)
        unit = str(attrs.get("unit_of_measurement") or "°C").strip()
        if val is not None:
            return f"- {entity_id} ({label}): {val:g}{unit}"
        return f"- {entity_id} ({label}): {raw}"

    if domain == "climate":
        parts: list[str] = [f"modo {raw}"]
        cur = attrs.get("current_temperature")
        tgt = attrs.get("temperature")
        if cur is not None:
            parts.append(f"leitura {cur}°C")
        if tgt is not None:
            parts.append(f"alvo {tgt}°C")
        return f"- {entity_id} ({label}): " + ", ".join(parts)

    if domain == "binary_sensor":
        if raw.lower() == "on":
            return f"- {entity_id} ({label}): ligado/on"
        if raw.lower() == "off":
            return f"- {entity_id} ({label}): desligado/off"

    if domain == "switch":
        on = raw.lower() in ("on", "true")
        return f"- {entity_id} ({label}): {'ligada' if on else 'desligada'}"

    val = _state_float(state)
    if val is not None and domain == "sensor":
        unit = str(attrs.get("unit_of_measurement") or "").strip()
        suffix = f" {unit}" if unit else ""
        return f"- {entity_id} ({label}): {val:g}{suffix}"

    return f"- {entity_id} ({label}): {raw}"


def scenario_by_id(catalog: DevicesCatalog, scenario_id: str) -> ScenarioConfig | None:
    for sc in catalog.scenarios:
        if sc.id == scenario_id:
            return sc
    return None


async def build_verified_states_context_block(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    scenario_id: str,
    *,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Bloco de estados HA para o Gemini (texto amigavel)."""
    from app.user_friendly import format_state_value

    scenario = scenario_by_id(catalog, scenario_id)
    if not scenario:
        return ""

    entity_ids = entity_ids_for_scenarios([scenario])
    if not entity_ids:
        return ""

    verified = await fetch_verified_entity_states(
        ha, entity_ids, states_map=states_map
    )
    lines = [
        "[Estados verificados agora no Home Assistant — use estes valores na resposta]",
        f"[Cenario aplicavel: {scenario.id}]",
        "",
    ]
    for eid in entity_ids:
        st = verified.get(eid)
        if st:
            lines.append(format_state_value(eid, st, catalog))
        else:
            lines.append(f"Nao consegui ler o estado desse aparelho agora.")
    return "\n".join(lines)


async def prepend_scenario_states_to_context(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    ctx: str,
    scenario_id: str,
    *,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    block = await build_verified_states_context_block(
        ha, catalog, scenario_id, states_map=states_map
    )
    if not block:
        return ctx
    return f"{block}\n\n{ctx}"


def build_gemini_scenario_correction_block(scenario_id: str) -> str:
    """Texto so para o contexto do Gemini — nunca deve ir ao WhatsApp."""
    return (
        "[Instrucao interna — nao repita nem parafraseie este bloco na resposta ao usuario]\n"
        f"Corrija: use action=reply, get_state ou call_service (nunca '{scenario_id}').\n"
        f"Siga o prompt do cenario [{scenario_id}] no catalogo em cache.\n"
        "Use os estados verificados abaixo. Responda em portugues, amigavel, sem entity_id.\n"
    )


async def build_friendly_reply_from_scenario(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    scenario_id: str,
    *,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Resumo legivel para WhatsApp quando o Gemini nao conclui (fallback final)."""
    from app.user_friendly import format_state_value

    scenario = scenario_by_id(catalog, scenario_id)
    if not scenario:
        return ""

    entity_ids = entity_ids_for_scenarios([scenario])
    if not entity_ids:
        return ""

    verified = await fetch_verified_entity_states(
        ha, entity_ids, states_map=states_map
    )
    lines: list[str] = []
    for eid in entity_ids:
        st = verified.get(eid)
        if not st:
            continue
        line = format_state_value(eid, st, catalog)
        if "indisponivel" in line.lower():
            continue
        lines.append(line)

    if not lines:
        return "Não consegui obter os dados do Home Assistant agora. Tente de novo em instantes."

    return "Situação atual:\n\n" + "\n".join(lines)
