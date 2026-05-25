"""Parse da resposta JSON do Gemini (objeto unico ou lista de acoes)."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_BATCH_KEYS = ("batch", "actions", "steps", "decisions")


def _strip_code_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


def parse_gemini_decision_payload(data: Any) -> list[dict[str, Any]]:
    """Normaliza resposta Gemini para lista de decisoes (>=1)."""
    if isinstance(data, list):
        out = [row for row in data if isinstance(row, dict) and row.get("action")]
        return out or [{"action": "reply", "response": "Não entendi a resposta."}]

    if isinstance(data, dict):
        for key in _BATCH_KEYS:
            nested = data.get(key)
            if isinstance(nested, list):
                out = [row for row in nested if isinstance(row, dict) and row.get("action")]
                if out:
                    return out
        if data.get("action"):
            return [data]

    return [{"action": "reply", "response": "Não entendi a resposta."}]


def parse_gemini_response_text(text: str) -> list[dict[str, Any]]:
    raw = _strip_code_fence(text)
    if not raw:
        return [{"action": "reply", "response": "Sem resposta do modelo."}]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Resposta Gemini nao-JSON: %s", raw[:300])
        return [{"action": "reply", "response": raw[:2000]}]
    return parse_gemini_decision_payload(data)


def wrap_decisions_for_handler(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Um dict para o pipeline existente, ou marcador de lote."""
    if len(decisions) == 1:
        return decisions[0]
    return {"action": "_batch", "batch": decisions}
