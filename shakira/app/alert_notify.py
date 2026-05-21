"""Resolucao de destinos WhatsApp para alertas (YAML + fallback HA)."""

from __future__ import annotations

from app.homeassistant import HomeAssistantClient
from app.whatsapp_phones import (
    ENTITY_PERMITTED,
    fetch_permitted_phones_raw,
    normalize_phone_digits,
    parse_allowed_numbers,
)


async def resolve_notify_phones(
    ha: HomeAssistantClient,
    *,
    phones: list[str] | None = None,
    default_phones: list[str] | None = None,
) -> list[str]:
    """
    Lista de numeros para envio de alertas.

    Uniao de default_notify (raiz do YAML) + notify.phones da regra.
    Se ambos vazios, usa input_text.whatsapp_bot_permitidos do HA.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for source in (default_phones or [], phones or []):
        for raw in source:
            digits = normalize_phone_digits(str(raw))
            if digits and digits not in seen:
                seen.add(digits)
                ordered.append(digits)
    if ordered:
        return sorted(ordered)

    raw = await fetch_permitted_phones_raw(ha)
    return sorted(parse_allowed_numbers(raw))


def permitted_entity_hint() -> str:
    return ENTITY_PERMITTED
