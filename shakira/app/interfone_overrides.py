"""Correcoes e detecao direta para historico do interfone."""

from __future__ import annotations

import re
from typing import Any

_INTERFONE_LIST_RE = re.compile(
    r"\b(?:"
    r"interfone|porteiro|campainha|doorbell"
    r"|hist[oó]rico\s+(?:do\s+)?interfone"
    r"|(?:últimas?|ultimas?)\s+chamadas?\s+(?:do\s+)?interfone"
    r"|chamadas?\s+do\s+interfone"
    r"|quem\s+tocou"
    r"|mostrar?\s+(?:o\s+)?(?:hist[oó]rico|registro)\s+(?:do\s+)?interfone"
    r"|ver\s+(?:o\s+)?(?:hist[oó]rico|registro)\s+(?:do\s+)?interfone"
    r")\b",
    re.I,
)


def looks_like_interfone_list_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(_INTERFONE_LIST_RE.search(text))


def try_interfone_list_decision_override(
    decision: dict[str, Any],
    *,
    user_text: str,
) -> dict[str, Any]:
    """Redireciona reply/get_state para interfone_list quando pedem historico."""
    if not looks_like_interfone_list_request(user_text):
        return decision

    action = str(decision.get("action") or "reply").strip().lower()
    if action == "interfone_list":
        return decision
    if action not in ("reply", "get_state", "list_entities"):
        return decision

    reply = str(decision.get("response") or "").strip()
    failed = "não consegui processar" in reply.lower()

    return {
        **decision,
        "action": "interfone_list",
        "response": (
            "Vou buscar o histórico de chamadas do interfone."
            if failed or not reply
            else reply
        ),
    }
