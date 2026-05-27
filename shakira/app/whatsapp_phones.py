"""Utilitarios de telefone WhatsApp (evita import circular com handlers)."""

from __future__ import annotations

import logging
import os
import threading
import time

from app.homeassistant import HomeAssistantClient

log = logging.getLogger(__name__)

ENTITY_PERMITTED = "input_text.whatsapp_bot_permitidos"

_permitted_cache_lock = threading.Lock()
_permitted_cache_raw: str | None = None
_permitted_cache_at: float = 0.0


def normalize_phone_digits(value: str) -> str:
    digits = "".join(c for c in value if c.isdigit())
    # Normalização de celulares do Brasil (nono dígito)
    # Se começa com 55 e tem 13 dígitos, e o 5º dígito (índice 4) é 9,
    # removemos o 5º dígito para normalizar de forma consistente para 12 dígitos.
    if digits.startswith("55") and len(digits) == 13:
        if digits[4] == "9":
            digits = digits[:4] + digits[5:]
    return digits


def parse_allowed_numbers(raw: str) -> set[str]:
    if not raw:
        return set()
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    out: set[str] = set()
    for p in parts:
        if not p:
            continue
        d = normalize_phone_digits(p)
        if d:
            out.add(d)
    return out


def _permitted_ttl_sec() -> float:
    return max(0.0, float(os.environ.get("SHAKIRA_PERMITTED_PHONES_CACHE_SEC", "60")))


async def fetch_permitted_phones_raw(ha: HomeAssistantClient) -> str:
    global _permitted_cache_raw, _permitted_cache_at
    ttl = _permitted_ttl_sec()
    if ttl > 0:
        with _permitted_cache_lock:
            if (
                _permitted_cache_raw is not None
                and time.monotonic() - _permitted_cache_at < ttl
            ):
                log.debug("Cache telefones permitidos hit (age=%.0fs)", time.monotonic() - _permitted_cache_at)
                return _permitted_cache_raw

    log.debug("Cache telefones permitidos miss — consultando HA")
    s = await ha.get_state(ENTITY_PERMITTED)
    raw = s["state"] if s and isinstance(s.get("state"), str) else ""

    if ttl > 0:
        with _permitted_cache_lock:
            _permitted_cache_raw = raw
            _permitted_cache_at = time.monotonic()
    return raw
