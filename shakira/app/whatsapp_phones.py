"""Utilitarios de telefone WhatsApp (evita import circular com handlers)."""

from __future__ import annotations

from app.homeassistant import HomeAssistantClient

ENTITY_PERMITTED = "input_text.whatsapp_bot_permitidos"


def normalize_phone_digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


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


async def fetch_permitted_phones_raw(ha: HomeAssistantClient) -> str:
    s = await ha.get_state(ENTITY_PERMITTED)
    if s and isinstance(s.get("state"), str):
        return s["state"]
    return ""
