"""Deteccao de cenarios do YAML e reforco do prompt enviado ao Gemini."""

from __future__ import annotations

import re

from app.devices_catalog import DevicesCatalog, ScenarioConfig

BATH_USER_RE = re.compile(
    r"\b(banho|banhar|água|agua|boiler)\b",
    re.IGNORECASE,
)

SERVER_USER_RE = re.compile(
    r"\b(servidor|servidores|cpu|mem[oó]ria|ram|disco|arm[aá]rio|ping|roteador|exaustor|omada|dvr|rede)\b",
    re.IGNORECASE,
)

ALARM_USER_RE = re.compile(
    r"\b(alarme|armar|desarmar|parti[cç][aã]o|amt)\b",
    re.IGNORECASE,
)


def _scenario_applies(scenario: ScenarioConfig, user_text: str) -> bool:
    sid = scenario.id.lower()
    if sid == "saude_servidor":
        return bool(SERVER_USER_RE.search(user_text or ""))
    if sid == "banho_boiler":
        return bool(BATH_USER_RE.search(user_text or ""))
    if sid == "alarme_casa":
        return bool(ALARM_USER_RE.search(user_text or ""))
    return False


def match_scenarios_for_message(user_text: str, catalog: DevicesCatalog) -> list[ScenarioConfig]:
    """Cenarios do YAML cuja mensagem do usuario se encaixa."""
    return [sc for sc in catalog.scenarios if _scenario_applies(sc, user_text)]


def _entity_ids_for_device_name(catalog: DevicesCatalog, name_hint: str) -> list[str]:
    hint = name_hint.lower()
    for dev in catalog.devices:
        if hint in dev.name.lower():
            return [e.entity_id for e in dev.entities]
    return []


def _related_entity_ids(scenario: ScenarioConfig, catalog: DevicesCatalog) -> list[str]:
    if scenario.id == "saude_servidor":
        return _entity_ids_for_device_name(catalog, "servidor")
    if scenario.id == "banho_boiler":
        return _entity_ids_for_device_name(catalog, "boiler")
    if scenario.id == "alarme_casa":
        return _entity_ids_for_device_name(catalog, "alarme")
    return []


def build_scenario_instruction_block(
    scenarios: list[ScenarioConfig], catalog: DevicesCatalog
) -> str:
    if not scenarios:
        return ""

    parts = [
        "[Instrucao do sistema — cenario(s) aplicavel(is) nesta mensagem]",
        "Os ESTADOS ATUAIS ja estao no mesmo pedido (bloco abaixo). Use-os diretamente.",
        "Responda com action=reply e texto COMPLETO em portugues.",
        "Nao diga apenas que vai verificar — entregue o resultado agora.",
        "",
    ]
    for sc in scenarios:
        parts.append(f"Cenario [{sc.id}]:")
        parts.append(sc.prompt.strip())
        related = _related_entity_ids(sc, catalog)
        if related:
            parts.append("Entidades principais: " + ", ".join(related))
        parts.append("")
    return "\n".join(parts).strip()


def augment_message_for_scenarios(user_text: str, catalog: DevicesCatalog) -> str:
    """Anexa instrucoes de cenario a mensagem do usuario (fluxo normal, sem bypass)."""
    base = (user_text or "").strip()
    matched = match_scenarios_for_message(base, catalog)
    if not matched:
        return base
    block = build_scenario_instruction_block(matched, catalog)
    return f"{base}\n\n{block}"
