"""Zonas AMT 8000 (sensor.amt_8000_zone_*) — setores do alarme, distintos das particoes."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.devices_catalog import DevicesCatalog
    from app.homeassistant import HomeAssistantClient

ZONE_ENTITY_RE = re.compile(r"^sensor\.amt_8000_zone_(\d+)$", re.IGNORECASE)

# Entidades de status da central, nao sensores fisicos de zona.
ZONE_ENTITY_SKIP = frozenset(
    {
        "sensor.amt_8000_zone_61",
    }
)

# Estados que indicam zona em disparo / violada (portas abertas, movimento, etc.).
ZONE_TRIGGERED_STATES = frozenset(
    {
        "open",
        "on",
        "triggered",
        "active",
        "violated",
        "alarm",
        "tamper",
        "motion",
        "detected",
    }
)

ZONE_NORMAL_STATES = frozenset(
    {
        "closed",
        "off",
        "inactive",
        "idle",
        "dry",
        "ok",
        "no_motion",
        "clear",
        "rest",
    }
)


def zone_entities_from_catalog(devices: DevicesCatalog | None) -> list[tuple[str, str]]:
    """Lista (entity_id, descricao) das zonas amt_8000 no shakira_devices.yaml."""
    if not devices:
        return []
    found: list[tuple[int, str, str]] = []
    for device in devices.devices:
        for ent in device.entities:
            eid = ent.entity_id.strip()
            if eid in ZONE_ENTITY_SKIP:
                continue
            m = ZONE_ENTITY_RE.match(eid)
            if not m:
                continue
            num = int(m.group(1))
            label = (ent.description or "").strip() or eid
            found.append((num, eid, label))
    found.sort(key=lambda row: row[0])
    return [(eid, label) for _, eid, label in found]


def zone_state_is_triggered(state: str) -> bool:
    s = (state or "").strip().lower()
    if not s or s in ("unknown", "unavailable"):
        return False
    if s in ZONE_NORMAL_STATES:
        return False
    if s in ZONE_TRIGGERED_STATES:
        return True
    return True


async def fetch_triggered_zones(
    ha: HomeAssistantClient,
    devices: DevicesCatalog | None,
) -> list[tuple[str, str, str]]:
    """Retorna [(entity_id, descricao, estado_ha), ...] das zonas em disparo."""
    triggered: list[tuple[str, str, str]] = []
    for entity_id, label in zone_entities_from_catalog(devices):
        state_data = await ha.get_state(entity_id)
        if not state_data:
            continue
        state = str(state_data.get("state", "")).strip()
        if zone_state_is_triggered(state):
            triggered.append((entity_id, label, state))
    return triggered


def build_trigger_message(
    partitions: list[tuple[str, str]],
    zones: list[tuple[str, str, str]],
) -> str:
    """
    Mensagem WhatsApp: Apenas setores (sensor.amt_8000_zone_*).
    Partições foram ocultadas para evitar poluição visual causada pelo disparo geral da central.
    """
    lines = ["ALERTA: alarme disparou!", ""]

    if zones:
        lines.append("Setores (zonas) em disparo:")
        for _, label, state in zones:
            state_bit = f" ({state})" if state else ""
            lines.append(f"• {label}{state_bit}")
        lines.append("")
    else:
        lines.append(
            "Setores (zonas): nenhum sensor de zona em disparo no momento."
        )
        lines.append("")

    if not partitions and not zones:
        return ""

    return "\n".join(lines).strip()
