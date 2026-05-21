"""Textos amigaveis para WhatsApp (sem jargao do Home Assistant)."""

from __future__ import annotations

import re
from typing import Any

from app.devices_catalog import DevicesCatalog

ENTITY_ID_RE = re.compile(
    r"\b(?:sensor|switch|input_select|lock|light|binary_sensor|number|climate|cover|fan|media_player)\."
    r"[a-z0-9_]+\b",
    re.IGNORECASE,
)

TECHNICAL_LINE_RE = re.compile(
    r"^\s*(\[?\{.*entity_id|input_select\.|call_service|domain\.|service_data|last_changed)",
    re.IGNORECASE | re.MULTILINE,
)

INTERNAL_INSTRUCTION_MARKERS = (
    "siga o prompt",
    "dados atuais (cenario",
    "conforme o cenario configurado",
    "vou consultar o",
    "[estados verificados",
    "[instrucao interna",
    "[correcao do sistema]",
    "[cenario aplicavel:",
    "no catalogo para responder",
    "no catalogo em cache",
    "rotina automatica no sistema",
    "nao use call_service manualmente",
    "não use call_service manualmente",
)


def is_internal_instruction_leak(text: str) -> bool:
    """Detecta texto de instrucao interna ou dump de cenario vazado para o usuario."""
    lower = (text or "").lower()
    if not lower.strip():
        return False
    if any(m in lower for m in INTERNAL_INSTRUCTION_MARKERS):
        return True
    if lower.startswith("- ") and "cenario" in lower and ("°c" in lower or " indisponivel" in lower):
        return True
    return False


def humanize_entity_id(entity_id: str) -> str:
    if "." in entity_id:
        entity_id = entity_id.split(".", 1)[1]
    return entity_id.replace("_", " ").strip().title()


def entity_display_name(
    entity_id: str,
    catalog: DevicesCatalog | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    if state:
        attrs = state.get("attributes") or {}
        if isinstance(attrs, dict):
            fn = attrs.get("friendly_name")
            if isinstance(fn, str) and fn.strip():
                return fn.strip()
    if catalog:
        ent = catalog.get_entity(entity_id)
        if ent and ent.description:
            desc = ent.description.strip()
            if desc:
                return desc.split("—")[0].split("-")[0].strip()[:80]
        for dev in catalog.devices:
            for ent in dev.entities:
                if ent.entity_id == entity_id:
                    if ent.description:
                        return ent.description.split("—")[0].split("-")[0].strip()[:80]
                    if dev.name:
                        return dev.name
    return humanize_entity_id(entity_id)


def format_state_value(entity_id: str, state: dict[str, Any], catalog: DevicesCatalog | None) -> str:
    raw = state.get("state")
    label = entity_display_name(entity_id, catalog, state)
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

    if raw in (None, "unknown", "unavailable", ""):
        return f"Não consegui obter o estado de {label} agora."

    if domain == "sensor":
        unit = ""
        attrs = state.get("attributes") or {}
        if isinstance(attrs, dict):
            u = attrs.get("unit_of_measurement")
            if u:
                unit = f" {u}"
        try:
            val = float(str(raw).replace(",", "."))
            if "temp" in entity_id.lower() or unit.strip() in ("°C", "C"):
                return f"A temperatura de {label} está em {val:g}°C."
            return f"{label}: {val:g}{unit}."
        except ValueError:
            pass

    if domain == "binary_sensor" and "ping" in entity_id.lower():
        if str(raw).lower() == "on":
            return f"{label}: online."
        if str(raw).lower() == "off":
            return f"{label}: offline."
        return f"{label}: {raw}."

    if domain == "climate":
        attrs = state.get("attributes") or {}
        parts: list[str] = []
        if isinstance(attrs, dict):
            cur = attrs.get("current_temperature")
            tgt = attrs.get("temperature")
            if cur is not None:
                parts.append(f"agora {cur:g}°C")
            if tgt is not None:
                parts.append(f"alvo {tgt:g}°C")
        extra = f" ({', '.join(parts)})" if parts else ""
        return f"{label}: {raw}{extra}."

    if domain in ("switch", "input_boolean", "light", "fan"):
        on = str(raw).lower() in ("on", "true", "ligado", "open", "unlocked")
        if on:
            return f"{label} está ligado(a)."
        return f"{label} está desligado(a)."

    if domain == "input_select":
        return f"{label} está em «{raw}»."

    if domain == "lock":
        if str(raw).lower() == "unlocked":
            return f"{label} está destrancada."
        if str(raw).lower() == "locked":
            return f"{label} está trancada."
        return f"{label}: {raw}."

    return f"{label}: {raw}."


def format_checking(entity_id: str, catalog: DevicesCatalog | None, state: dict[str, Any] | None = None) -> str:
    label = entity_display_name(entity_id, catalog, state)
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain == "sensor" and "temp" in entity_id.lower():
        return "Vou verificar a temperatura da agua..."
    return f"Vou verificar {label}..."


def format_action_in_progress(
    domain: str,
    service: str,
    entity_id: str,
    service_data: dict[str, Any] | None,
    catalog: DevicesCatalog | None,
    state: dict[str, Any] | None = None,
) -> str:
    label = entity_display_name(entity_id, catalog, state)
    data = service_data or {}
    option = data.get("option")

    if domain == "input_select" and service == "select_option" and option:
        if "boiler" in entity_id.lower() or "boiler" in label.lower():
            if str(option).lower() == "ligado":
                return "Vou ligar o aquecimento do boiler..."
        return f"Vou ajustar {label} para «{option}»..."

    if domain == "switch" and service == "turn_on":
        return f"Vou ligar {label}..."
    if domain == "switch" and service == "turn_off":
        return f"Vou desligar {label}..."

    if domain == "light" and service == "turn_on":
        pct = data.get("brightness_pct")
        bri = data.get("brightness")
        if pct is not None:
            return f"Vou ajustar {label} para {pct}%..."
        if bri is not None:
            return f"Vou ajustar a intensidade de {label}..."
        return f"Vou acender {label}..."
    if domain == "light" and service == "turn_off":
        return f"Vou apagar {label}..."

    if domain == "lock" and service == "unlock":
        return f"Vou destrancar {label}..."
    if domain == "lock" and service == "lock":
        return f"Vou trancar {label}..."

    return f"Um momento, estou a tratar de {label}..."


def _state_from_service_result(result: Any) -> str | None:
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and "state" in first:
            return str(first["state"])
    if isinstance(result, dict) and "state" in result:
        return str(result["state"])
    return None


def format_action_success(
    domain: str,
    service: str,
    entity_id: str,
    service_data: dict[str, Any] | None,
    catalog: DevicesCatalog | None,
    result: Any = None,
    state_after: dict[str, Any] | None = None,
) -> str:
    label = entity_display_name(entity_id, catalog, state_after)
    data = service_data or {}
    new_state = None
    if state_after:
        new_state = state_after.get("state")
    if new_state is None:
        new_state = _state_from_service_result(result)

    if domain == "input_select" and service == "select_option":
        opt = new_state or data.get("option")
        if opt and ("boiler" in entity_id.lower() or "boiler" in label.lower()):
            if str(opt).lower() == "ligado":
                return "Pronto! Liguei o aquecimento do boiler."
        if opt:
            return f"Pronto! {label} está em «{opt}»."
        return f"Pronto! Ajustei {label}."

    if domain == "switch" and service == "turn_on":
        return f"Pronto! {label} ligado(a)."
    if domain == "switch" and service == "turn_off":
        return f"Pronto! {label} desligado(a)."

    if domain == "light" and service == "turn_on":
        pct = data.get("brightness_pct")
        if pct is not None:
            return f"Pronto! {label} ajustado(a) para {pct}%."
        if data.get("brightness") is not None:
            return f"Pronto! Intensidade de {label} ajustada."
        return f"Pronto! {label} aceso(a)."
    if domain == "light" and service == "turn_off":
        return f"Pronto! {label} apagado(a)."

    if domain == "lock" and service == "unlock":
        return f"Pronto! {label} destrancada."
    if domain == "lock" and service == "lock":
        return f"Pronto! {label} trancada."

    return f"Pronto! Alteração feita em {label}."


def format_ha_error_user() -> str:
    return "Não consegui completar essa ação agora. Tente de novo em instantes."


def format_whatsapp_layout(text: str) -> str:
    """Garante quebras de linha em listas numeradas ou com marcadores no WhatsApp."""
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # Introducao antes de lista numerada: "...guardado: 1. Item"
    t = re.sub(r"([.:!?])\s+(\d+\.\s)", r"\1\n\n\2", t)
    # Entre itens: "...fotos) 2. Outro"
    t = re.sub(r"(\S)\s+(\d+\.\s)", r"\1\n\2", t)
    # Listas com marcador apos pontuacao
    t = re.sub(r"([.:!?])\s+([•\-–]\s+)", r"\1\n\n\2", t)
    t = re.sub(r"(\S)\s+([•\-–]\s+)", r"\1\n\2", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def polish_user_message(text: str) -> str:
    """Remove termos tecnicos e dumps JSON antes de enviar ao WhatsApp."""
    t = (text or "").strip()
    if not t:
        return ""

    if is_internal_instruction_leak(t):
        return ""

    if t.startswith("[{") or t.startswith("[{'") or "'entity_id'" in t[:200]:
        return ""

    if TECHNICAL_LINE_RE.search(t):
        lines = [ln for ln in t.splitlines() if not TECHNICAL_LINE_RE.search(ln)]
        t = "\n".join(lines).strip()

    t = ENTITY_ID_RE.sub(lambda m: entity_display_name(m.group(0)), t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"\bHome Assistant\b", "casa", t, flags=re.IGNORECASE)
    t = re.sub(r"\bentity_id\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\binput_select\.select_option\b", "", t, flags=re.IGNORECASE)
    # Preserva quebras de linha; so normaliza espacos dentro de cada linha.
    lines_out: list[str] = []
    for ln in t.split("\n"):
        if not ln.strip():
            lines_out.append("")
        else:
            lines_out.append(re.sub(r"[ \t]{2,}", " ", ln.strip()))
    t = "\n".join(lines_out)
    t = format_whatsapp_layout(t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t
