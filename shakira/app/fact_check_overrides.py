"""Correcoes quando Gemini recusa ou nao usa fact_check_claim."""

from __future__ import annotations

import re
from typing import Any

from app.config import AppSettings
from app.fact_check_actions import fact_check_configured

_FACT_CHECK_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:verifique|verifica|checa|checar|cheque|confira|confere|confirme|confirma)\s+(?:se\s+)?(?:\w+\s+){0,2}(?:[eé]\s+verdade\s+que|[eé]\s+fake|procede|e\s+mentira|boato|rumor|not[ií]cia|alega[cç][aã]o|fact[\s-]?check|fake\s*news|veracidade|informa[cç][aã]o)\b"
    r"|\b[eé]\s+verdade\s+que\b"
    r"|\bisso\s+[eé]\s+verdade\b"
    r"|\bisso\s+procede\b"
    r"|\bfake\s*news\b"
    r"|\bfact[\s-]?check\b"
    r"|\b(?:desminta|desmentir)\b"
    r"|\b(?:not[ií]cia|alega[cç][aã]o|boato|rumor|informa[cç][aã]o)\b.*\b(?:verdade|verificar|confirmar|checar|confira|cheque)\b"
    r"|\b(?:verificar|confirmar|checar|confira|cheque)\b.*\b(?:not[ií]cia|alega[cç][aã]o|boato|rumor|informa[cç][aã]o)\b"
    r")",
    re.I,
)

_REFUSAL_REPLY_RE = re.compile(
    r"n[aã]o tenho (?:informa[cç][oõ]es|conhecimento)|"
    r"sou (?:um )?(?:sistema de )?automa[cç][aã]o|"
    r"minhas fun[cç][oõ]es s[aã]o|"
    r"n[aã]o (?:tenho|posso) (?:como )?verificar|"
    r"n[aã]o tenho acesso",
    re.I,
)

_HOME_STATE_RE = re.compile(
    r"\b(?:port(?:[aã]o|[oõ]es)|porta(?:s)?(?:\s+social)?|fechadura(?:s)?|luz(?:es)?|boiler(?:s)?|geladeira(?:s)?|"
    r"temperatura(?:\s+da\s+[aá]gua|\s+do\s+boiler)?|umidade|sensor(?:es)?|"
    r"c[âa]mera(?:s)?|garagem|garagens|interruptor(?:es)?|tomada(?:s)?|ar[\s-]condicionado|"
    r"home assistant|dispositivo(?:s)?\s+(?:da\s+)?casa|rua(?:s)?|quintal|jardim|jardins|"
    r"interfone(?:s)?|campainha(?:s)?|planta(?:s)?|vaso(?:s)?|aspirador(?:es)?|rob[oô](?:s)?|"
    r"chuva(?:s)?|toldo(?:s)?)\b",
    re.I,
)

_QUERY_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:verifique|verifica|checa|checar|cheque|confira|confere|confirme|confirma)\s+"
    r"(?:se\s+)?(?:[eé]\s+verdade\s+que\s+)?"
    r"|(?:pode\s+)?(?:confirmar|verificar|checar|confira)\s+(?:essa\s+)?(?:not[ií]cia|alega[cç][aã]o|boato)[:\s]+"
    r"|(?:desminta|desmentir)\s+(?:isso|essa\s+not[ií]cia)[:\s]*"
    r"|isso\s+[eé]\s+verdade[:\s]*"
    r"|isso\s+procede[:\s]*"
    r")",
    re.I,
)

_VERDADE_QUE_RE = re.compile(r"[eé]\s+verdade\s+que\s+(.+)$", re.I)


def extract_fact_check_query_from_user_text(text: str) -> str:
    """Extrai a alegacao a verificar a partir da mensagem do usuario."""
    raw = (text or "").strip()
    if not raw:
        return ""

    oneline = " ".join(raw.split())
    m = _VERDADE_QUE_RE.search(oneline)
    if m:
        return m.group(1).strip(" ?.")

    if re.match(r"isso\s+[eé]\s+verdade", oneline, re.I):
        parts = re.split(r"\n+", raw, maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            return parts[1].strip()

    cleaned = _QUERY_PREFIX_RE.sub("", oneline).strip(" ?.")
    return cleaned or oneline


def _looks_like_fact_check_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or _HOME_STATE_RE.search(text):
        return False
    return bool(_FACT_CHECK_INTENT_RE.search(text))


def try_fact_check_decision_override(
    decision: dict[str, Any],
    *,
    user_text: str,
    settings: AppSettings,
) -> dict[str, Any]:
    """Redireciona reply errado para fact_check_claim quando detectavel."""
    if not fact_check_configured(settings):
        return decision

    action = str(decision.get("action") or "reply").strip().lower()
    reply = str(decision.get("response") or "").strip()
    wants_check = _looks_like_fact_check_request(user_text)
    refused = bool(reply and _REFUSAL_REPLY_RE.search(reply))

    if action == "fact_check_claim":
        if not str(decision.get("fact_check_query") or "").strip():
            query = extract_fact_check_query_from_user_text(user_text)
            if query:
                return {**decision, "fact_check_query": query}
        return decision

    if not wants_check and not refused:
        return decision

    should_override = (wants_check and action != "fact_check_claim") or (
        refused and action == "reply"
    )
    if not should_override:
        return decision

    query = extract_fact_check_query_from_user_text(user_text)
    if not query:
        return decision

    return {
        **decision,
        "action": "fact_check_claim",
        "fact_check_query": query,
        "response": "Vou consultar verificadores de fact-check...",
    }
