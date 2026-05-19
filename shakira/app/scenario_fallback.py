"""Fallback minimo quando o Gemini usa o id do cenario como action."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.devices_catalog import DevicesCatalog, ScenarioConfig
from app.homeassistant import HomeAssistantClient
from app.scenario_context import (
    entity_ids_for_scenarios,
    fetch_verified_entity_states,
    format_entity_state_for_prompt,
)

log = logging.getLogger(__name__)

OnStep = Callable[[str], Awaitable[None]]


def _scenario_by_id(catalog: DevicesCatalog, scenario_id: str | None) -> ScenarioConfig | None:
    if not scenario_id:
        return None
    for sc in catalog.scenarios:
        if sc.id == scenario_id:
            return sc
    return None


async def _emit(text: str, on_step: OnStep | None) -> None:
    if on_step:
        await on_step(text)


async def try_scenario_fallback_reply(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    user_text: str = "",
    history_text: str = "",
    scenario_id: str | None = None,
    on_step: OnStep | None = None,
) -> str | None:
    """So quando o Gemini confundiu id de cenario com action — le entidades do prompt no HA."""
    _ = user_text, history_text
    scenario = _scenario_by_id(catalog, scenario_id)
    if not scenario:
        return None

    entity_ids = entity_ids_for_scenarios([scenario])
    if not entity_ids:
        return None

    await _emit("Vou consultar o Home Assistant conforme o cenario configurado...", on_step)
    verified = await fetch_verified_entity_states(ha, entity_ids)
    lines = [
        format_entity_state_for_prompt(eid, verified.get(eid), catalog) for eid in entity_ids
    ]
    msg = "\n".join(
        [
            f"Dados atuais (cenario {scenario.id}):",
            "",
            *lines,
            "",
            "Siga o prompt deste cenario no catalogo para responder ao usuario.",
        ]
    )
    await _emit(msg, on_step)
    return None if on_step else msg
