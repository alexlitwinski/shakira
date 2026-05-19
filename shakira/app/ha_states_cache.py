"""Cache TTL em memoria para estados do Home Assistant."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_cache_lock = threading.Lock()
_all_states: list[dict[str, Any]] | None = None
_all_states_at: float = 0.0
_by_id: dict[str, dict[str, Any]] | None = None
_by_id_at: float = 0.0


def _ttl_sec() -> float:
    return max(0.0, float(os.environ.get("SHAKIRA_HA_STATES_CACHE_SEC", "10")))


def invalidate_ha_states_cache() -> None:
    global _all_states, _all_states_at, _by_id, _by_id_at
    with _cache_lock:
        _all_states = None
        _all_states_at = 0.0
        _by_id = None
        _by_id_at = 0.0


def store_all_states(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Guarda snapshot completo e devolve mapa entity_id -> state."""
    global _all_states, _all_states_at, _by_id, _by_id_at
    now = time.monotonic()
    by_id = {
        str(s.get("entity_id", "")): s
        for s in states
        if s.get("entity_id")
    }
    with _cache_lock:
        _all_states = states
        _all_states_at = now
        _by_id = by_id
        _by_id_at = now
    return by_id


def get_all_states_cached() -> list[dict[str, Any]] | None:
    ttl = _ttl_sec()
    if ttl <= 0:
        return None
    with _cache_lock:
        if _all_states is not None and time.monotonic() - _all_states_at < ttl:
            return _all_states
    return None


def get_states_map_cached() -> dict[str, dict[str, Any]] | None:
    ttl = _ttl_sec()
    if ttl <= 0:
        return None
    with _cache_lock:
        if _by_id is not None and time.monotonic() - _by_id_at < ttl:
            return dict(_by_id)
    return None


def filter_states_for_ids(
    states: list[dict[str, Any]], entity_ids: list[str]
) -> list[dict[str, Any]]:
    if not entity_ids:
        return []
    wanted = set(entity_ids)
    return [s for s in states if s.get("entity_id") in wanted]
