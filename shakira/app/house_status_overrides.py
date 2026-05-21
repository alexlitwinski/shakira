"""Correcoes quando Gemini responde com reply em vez de house_status."""

from __future__ import annotations

import re
from typing import Any

_HOUSE_STATUS_RE = re.compile(
    r"\b(?:"
    r"como\s+(?:est[aá]|t[aá]|ta)\s+(?:a\s+)?casa(?:\s+agora)?"
    r"|situa[cç][aã]o\s+(?:da\s+)?casa"
    r"|como\s+(?:est[aá]|t[aá]|ta)\s+em\s+casa"
    r"|(?:tudo\s+)?(?:tranquilo|normal|ok)\s+(?:em\s+)?casa"
    r"|(?:tem|h[aá])\s+alg(?:um|uma)\s+coisa\s+(?:estranha|acontecendo)"
    r"|(?:o\s+)?que\s+(?:est[aá]|t[aá]|ta)\s+acontecendo(?:\s+(?:em\s+)?casa)?"
    r"|resumo\s+(?:da\s+)?casa"
    r"|verif(?:icar|ique)\s+(?:como\s+(?:est[aá]|t[aá]|ta)\s+)?(?:a\s+)?casa"
    r"|(?:tem|h[aá])\s+algu[eé]m(?:\s+(?:em\s+)?casa)?"
    r")\b",
    re.I,
)

_SERVER_ONLY_RE = re.compile(
    r"\b(?:servidor|mem[oó]ria|cpu|disco|arm[aá]rio\s+de\s+servidores)\b",
    re.I,
)


def looks_like_house_status_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _SERVER_ONLY_RE.search(text) and not _HOUSE_STATUS_RE.search(text):
        return False
    return bool(_HOUSE_STATUS_RE.search(text))


def try_house_status_decision_override(
    decision: dict[str, Any],
    *,
    user_text: str,
) -> dict[str, Any]:
    """Redireciona reply/get_state para house_status quando o pedido e situacao geral da casa."""
    if not looks_like_house_status_request(user_text):
        return decision

    action = str(decision.get("action") or "reply").strip().lower()
    if action == "house_status":
        return decision
    if action not in ("reply", "get_state", "list_entities"):
        return decision

    return {
        **decision,
        "action": "house_status",
        "response": "Vou verificar como está a casa agora.",
    }
