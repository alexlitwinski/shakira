"""Desambigua 'sim' e outras confirmacoes curtas usando a ultima mensagem do assistente."""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

from app.conversation_history import HistoryEntry
from app.devices_catalog import DevicesCatalog

AFFIRMATIVE_SHORT_RE = re.compile(
    r"^\s*(sim|s|yes|ok|pode|confirmo|isso|essa|esse|esta|este)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

_DOOR_HINTS = (
    "porta",
    "destranc",
    "trancar",
    "destravar",
    "abrir a porta",
    "fechadura",
    "portao",
    "portão",
    "portao social",
    "portão social",
    "código de acesso",
    "senha para",
    "porta social",
    "porta principal",
    "entrar em casa",
)
_BOILER_HINTS = (
    "boiler",
    "aquec",
    "banho",
    "água do banho",
    "água quente",
    "temperatura da água",
)


def is_affirmative_short(user_text: str) -> bool:
    return bool(AFFIRMATIVE_SHORT_RE.match((user_text or "").strip()))


def last_assistant_text(entries: list[HistoryEntry]) -> str:
    for entry in reversed(entries):
        if entry.role == "assistant":
            return entry.text
    return ""


def classify_last_assistant_topic(last_assistant: str) -> str | None:
    low = (last_assistant or "").lower()
    if not low.strip():
        return None
    if any(h in low for h in _DOOR_HINTS):
        return "door"
    if any(h in low for h in _BOILER_HINTS):
        return "boiler"
    return None


def augment_user_message_for_affirmative(
    user_text: str, history_entries: list[HistoryEntry]
) -> str:
    """Reforco no pedido ao Gemini: 'sim' vale para a ultima pergunta do assistente."""
    base = (user_text or "").strip()
    if not is_affirmative_short(base):
        return base

    last = last_assistant_text(history_entries)
    topic = classify_last_assistant_topic(last)
    if topic == "door":
        return (
            f"{base}\n\n"
            "[Confirmação do usuário] Sim — referente à sua ÚLTIMA mensagem sobre porta "
            "(abrir/destrancar). Prossiga com a porta social se foi isso que você ofereceu. "
            "Não execute ações de boiler, banho ou outros assuntos antigos do histórico."
        )
    if topic == "boiler":
        return (
            f"{base}\n\n"
            "[Confirmação do usuário] Sim — referente à sua ÚLTIMA mensagem sobre boiler, "
            "banho ou aquecer a água. Ignore pedidos mais antigos sobre outros assuntos."
        )
    return (
        f"{base}\n\n"
        "[Confirmação do usuário] Sim — execute somente o que você perguntou na mensagem "
        "imediatamente anterior. Ignore cenários ou pedidos mais antigos no histórico."
    )


def _decision_target_entity(decision: dict[str, Any]) -> str:
    eid = decision.get("entity_id")
    if isinstance(eid, str) and eid.strip():
        return eid.strip().lower()
    svc = decision.get("service_data")
    if isinstance(svc, dict):
        raw = svc.get("entity_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return ""


def correct_affirmative_misroute(
    decision: dict[str, Any],
    *,
    user_text: str,
    history_entries: list[HistoryEntry],
    catalog: DevicesCatalog,
) -> dict[str, Any]:
    """Corrige Gemini que ligou boiler apos 'sim' a pergunta sobre porta."""
    if not is_affirmative_short(user_text):
        return decision

    if classify_last_assistant_topic(last_assistant_text(history_entries)) != "door":
        return decision

    target = _decision_target_entity(decision)
    domain = str(decision.get("domain") or "").lower()

    if "boiler" not in target and domain != "input_select":
        return decision

    door_id = "lock.porta_social"
    if door_id not in catalog.actionable_entity_ids():
        return decision

    log.warning(
        "Corrigindo confirmacao: usuario disse sim apos pergunta sobre porta; "
        "Gemini tentou boiler"
    )

    return {
        "action": "call_service",
        "domain": "lock",
        "service": "unlock",
        "service_data": {"entity_id": door_id},
        "response": "Vou destrancar a porta social.",
    }
