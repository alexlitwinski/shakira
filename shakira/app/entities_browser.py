"""Listagem de entidades HA para o painel Ingress (browser + copiar IDs)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.client import connect

from app.config import AppSettings
from app.devices_catalog import DevicesCatalog
from app.ha_states_cache import (
    get_all_states_cached,
    invalidate_ha_states_cache,
    store_all_states,
)
from app.ha_websocket import ha_websocket_url
from app.homeassistant import HomeAssistantClient

log = logging.getLogger(__name__)


async def _fetch_registry_meta(settings: AppSettings) -> dict[str, dict[str, Any]]:
    """entity_id -> platform, device_class, disabled (via WebSocket HA)."""
    token = settings.supervisor_token
    if not token:
        return {}

    url = ha_websocket_url(settings.ha_url)
    try:
        async with connect(url, open_timeout=15, close_timeout=5) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            if json.loads(raw).get("type") != "auth_required":
                return {}

            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            if json.loads(raw).get("type") != "auth_ok":
                return {}

            req_id = 1
            await ws.send(
                json.dumps({"id": req_id, "type": "config/entity_registry/list"})
            )

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                msg = json.loads(raw)
                if msg.get("type") != "result" or msg.get("id") != req_id:
                    continue
                if not msg.get("success"):
                    log.warning("entity_registry/list falhou: %s", msg.get("error"))
                    return {}
                out: dict[str, dict[str, Any]] = {}
                for ent in msg.get("result") or []:
                    eid = ent.get("entity_id")
                    if not eid:
                        continue
                    out[str(eid)] = {
                        "platform": str(ent.get("platform") or ""),
                        "device_class": ent.get("device_class") or "",
                        "disabled": bool(ent.get("disabled_by")),
                    }
                return out
    except Exception as e:
        log.warning("Entity registry indisponivel: %s", e)
    return {}


GENERIC_DOMAINS = {
    "sensor", "binary_sensor", "light", "switch", "cover", "climate", "camera",
    "media_player", "number", "select", "button", "input_boolean", "input_select",
    "input_number", "input_text", "input_datetime", "scene", "script", "automation",
    "group", "zone", "device_tracker", "person", "sun", "weather", "todo",
    "update", "text", "valve", "event", "fan", "humidifier", "lawn_mower",
    "lock", "siren", "vacuum", "water_heater"
}


def _serialize_entity(
    state: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    catalog_ids: set[str],
    components: list[str] | None = None,
) -> dict[str, Any]:
    eid = str(state.get("entity_id") or "")
    domain = eid.split(".", 1)[0] if "." in eid else ""
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    reg = registry.get(eid) or {}

    platform = (
        str(reg.get("platform") or "")
        or str(attrs.get("platform") or "")
        or str(attrs.get("source_type") or "")
    )

    # Fallback inteligente se a plataforma nao foi definida e temos a lista de componentes
    if not platform and components:
        object_id = eid.split(".", 1)[1] if "." in eid else ""
        for comp in components:
            if comp in GENERIC_DOMAINS:
                continue
            if object_id.startswith(f"{comp}_") or object_id == comp:
                platform = comp
                break

    device_class = str(reg.get("device_class") or attrs.get("device_class") or "")

    return {
        "entity_id": eid,
        "state": str(state.get("state") or ""),
        "friendly_name": str(attrs.get("friendly_name") or ""),
        "domain": domain,
        "platform": platform,
        "device_class": device_class,
        "disabled": bool(reg.get("disabled")),
        "in_catalog": eid in catalog_ids,
    }


async def build_entities_payload(
    *,
    ha: HomeAssistantClient,
    settings: AppSettings,
    catalog: DevicesCatalog,
    refresh: bool = False,
) -> dict[str, Any]:
    if refresh:
        invalidate_ha_states_cache()

    cached = get_all_states_cached()
    from_cache = cached is not None
    if cached is None:
        cached = await ha.get_states()
        store_all_states(cached)

    registry = await _fetch_registry_meta(settings)
    
    # Busca componentes do HA como fallback
    components = []
    ha_config = await ha.get_config()
    if ha_config and isinstance(ha_config.get("components"), list):
        components = sorted(
            [c for c in ha_config["components"] if isinstance(c, str) and "." not in c],
            key=len,
            reverse=True,
        )

    catalog_ids = set(catalog.entity_map().keys())

    entities = [
        _serialize_entity(st, registry, catalog_ids, components)
        for st in cached
        if isinstance(st, dict) and st.get("entity_id")
    ]
    entities.sort(key=lambda e: e["entity_id"])

    domains = sorted({e["domain"] for e in entities if e["domain"]})
    platforms = sorted({e["platform"] for e in entities if e["platform"]})

    return {
        "entities": entities,
        "count": len(entities),
        "domains": domains,
        "platforms": platforms,
        "cached": from_cache and not refresh,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
