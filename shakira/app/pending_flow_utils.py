"""TTL e detecao de mudanca de assunto em fluxos pendentes (multi-passo)."""

from __future__ import annotations

import os
import re
import time

_URL_IN_TEXT_RE = re.compile(r"https?://|www\.", re.I)

PENDING_FLOW_TTL_SEC = float(os.environ.get("SHAKIRA_PENDING_FLOW_TTL_SEC", "1800"))


def is_pending_expired(created_at: float, *, ttl_sec: float | None = None) -> bool:
    if not created_at:
        return False
    ttl = PENDING_FLOW_TTL_SEC if ttl_sec is None else ttl_sec
    return time.monotonic() - created_at > ttl


def message_changes_conversation_topic(text: str) -> bool:
    """True se a mensagem claramente nao e resposta ao menu/pergunta pendente."""
    t = (text or "").strip()
    if not t:
        return False
    if _URL_IN_TEXT_RE.search(t):
        return True
    try:
        from app.instagram_links_parser import extract_instagram_urls

        if extract_instagram_urls(t):
            return True
    except ImportError:
        pass
    if re.search(r"\binstagram\.com\b", t, re.I):
        return True
    return False


def should_abandon_pending_flow(
    created_at: float,
    text: str,
    *,
    pending_kind: str = "menu",
    ttl_sec: float | None = None,
) -> bool:
    """
    pending_kind:
      - menu: opcoes numeradas (portao fallback/followup, cofre pick)
      - password: codigo numerico (portao social, unlock HA)
      - text: resposta textual curta (instagram descricao, cofre label)
    """
    if is_pending_expired(created_at, ttl_sec=ttl_sec):
        return True
    if message_changes_conversation_topic(text):
        return True
    t = (text or "").strip()
    if pending_kind == "menu" and len(t) > 80:
        return True
    if pending_kind == "password":
        if re.fullmatch(r"\d{4,8}", t):
            return False
        if re.match(r"^\s*(cancelar|cancela|nao|não|desistir)\b", t, re.I):
            return False
        if len(t) <= 12 and t.isdigit():
            return False
        if len(t) > 24:
            return True
    return False
