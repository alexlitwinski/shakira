"""Detecta pedidos de gravar credenciais e extrai rotulo/senha do texto."""

from __future__ import annotations

import re
from typing import Any

_CREDENTIAL_NOUN_RE = re.compile(
    r"\b(?:senha|password|pin|c[oó]digo|credencial|wifi|wi-fi)\b",
    re.I,
)

_SAVE_VERB_RE = re.compile(
    r"\b(?:guardar?|grava(?:r)?|salva(?:r)?|anotar?|anota|"
    r"lembra(?:r|me)?|memoriz(?:a|ar)|registr(?:a|ar))\b",
    re.I,
)

_EXPLICIT_SAVE_RE = re.compile(
    r"\b(?:guardar?|grava(?:r)?|salva(?:r)?|anotar?)\b.*\b(?:senha|credencial)\b",
    re.I,
)

_RETRIEVE_START_RE = re.compile(
    r"^\s*(?:qual|quais|mostr(?:e|a)|recuper(?:e|a)|busc(?:e|a)|"
    r"me\s+(?:d[ae]|manda|envia)|envi(?:e|a)|preciso\s+da?)\b",
    re.I,
)

_HA_LOCK_PASSWORD_RE = re.compile(
    r"\b(?:destranc|tranc|abre|fecha|porta|portão|portao|fechadura|lock)\b",
    re.I,
)

_LABEL_SECRET_PATTERNS = (
    re.compile(
        r"\b(?:senha|password|pin|c[oó]digo|credencial|wifi|wi-fi)\s+"
        r"(?:da|do|de|para)\s+(.+?)\s*(?:é|e|:|=)\s*(.+)$",
        re.I,
    ),
    re.compile(
        r"\b(?:senha|password|pin|c[oó]digo|credencial|wifi|wi-fi)\s*"
        r"(?:é|e|:|=)\s*(.+)$",
        re.I,
    ),
)

_LABEL_ONLY_RE = re.compile(
    r"\b(?:senha|password|pin|c[oó]digo|credencial|wifi|wi-fi)\s+"
    r"(?:da|do|de|para)\s+(.+?)\s*$",
    re.I,
)


def _clean_label(raw: str) -> str:
    label = (raw or "").strip().strip(" .!?,;:")
    label = re.sub(r"^(?:da|do|de|a|o|para)\s+", "", label, flags=re.I).strip()
    return label[:120]


def _clean_secret(raw: str) -> str:
    return (raw or "").strip().strip('"').strip("'")[:200]


def is_ha_lock_context(text: str) -> bool:
    """Pedido sobre fechadura/porta HA, nao credencial de cofre."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"\b(?:cofre|wifi|wi-fi|conta|site|servi[cç]o|email|e-mail)\b", t, re.I):
        return False
    return bool(_HA_LOCK_PASSWORD_RE.search(t))


def is_vault_save_intent(text: str) -> bool:
    """True se o utilizador quer gravar credencial no cofre."""
    t = (text or "").strip()
    if not t:
        return False
    if _RETRIEVE_START_RE.search(t) and not _SAVE_VERB_RE.search(t):
        return False
    if _EXPLICIT_SAVE_RE.search(t):
        return True
    if _SAVE_VERB_RE.search(t) and _CREDENTIAL_NOUN_RE.search(t):
        return True
    label, secret = parse_credential_save(t)
    return bool(label and secret)
    # Nota: label sem secret e tratado em classify_vault_intent via parse parcial


def parse_credential_save(text: str) -> tuple[str, str]:
    """Extrai (rotulo, senha) de frases como 'senha do wifi é abc123'."""
    t = (text or "").strip()
    if not t:
        return "", ""

    for pattern in _LABEL_SECRET_PATTERNS:
        m = pattern.search(t)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 2:
            label = _clean_label(groups[0])
            secret = _clean_secret(groups[1])
            if label and secret:
                return label, secret
        if len(groups) == 1:
            secret = _clean_secret(groups[0])
            label_m = _LABEL_ONLY_RE.search(t)
            label = _clean_label(label_m.group(1)) if label_m else ""
            if secret and (label or _CREDENTIAL_NOUN_RE.search(t)):
                return label or "credencial", secret

    m = _LABEL_ONLY_RE.search(t)
    if m and _SAVE_VERB_RE.search(t):
        return _clean_label(m.group(1)), ""

    return "", ""


def memory_decision_looks_like_vault(decision: dict[str, Any]) -> bool:
    """True se save_memory do Gemini devia ir para o cofre."""
    text = str(decision.get("memory_text") or "").strip()
    label = str(decision.get("memory_label") or "").strip()
    combined = f"{label} {text}".strip()
    if not combined:
        return False
    if is_vault_save_intent(combined):
        return True
    if not _CREDENTIAL_NOUN_RE.search(combined):
        return False
    parsed_label, parsed_secret = parse_credential_save(combined)
    if parsed_label and parsed_secret:
        return True
    blob = combined.casefold()
    if any(k in blob for k in ("senha", "password", " pin ", "credencial", "wifi")):
        return True
    return False


def vault_fields_from_memory_decision(decision: dict[str, Any]) -> tuple[str, str]:
    text = str(decision.get("memory_text") or "").strip()
    label = str(decision.get("memory_label") or "").strip()
    combined = f"{label} {text}".strip()
    parsed_label, parsed_secret = parse_credential_save(combined)
    if not parsed_label and label:
        parsed_label = _clean_label(label)
    if not parsed_label and text:
        m = _LABEL_ONLY_RE.search(text)
        if m:
            parsed_label = _clean_label(m.group(1))
    return parsed_label, parsed_secret
