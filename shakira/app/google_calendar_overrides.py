"""Correcoes quando Gemini nao usa as actions corretas da agenda Google."""

from __future__ import annotations

import re
from typing import Any

from app.google_calendar_parser import extract_google_calendar_urls
from app.google_calendar_store import get_google_calendar_store

_CALENDAR_VIEW_RE = re.compile(
    r"\b(?:calend[aá]rio|agenda|compromissos?|eventos?|reuni[oõ]es?)\b",
    re.I,
)
_MISCONFIGURED_REPLY_RE = re.compile(
    r"n[aã]o configurei|nao configurei|ainda n[aã]o tenho|preciso.*link|"
    r"link p[uú]blico|forne[cç]a o link|me diga o link",
    re.I,
)
_ECHO_LINK_REPLY_RE = re.compile(
    r"calendar\.google\.com|siga este link|acesse.*link",
    re.I,
)


def try_google_calendar_decision_override(
    decision: dict[str, Any],
    *,
    phone: str,
    user_text: str,
) -> dict[str, Any]:
    """Redireciona reply errado para save/list quando detectavel."""
    text = (user_text or "").strip()
    urls = extract_google_calendar_urls(text)
    action = str(decision.get("action") or "reply").strip().lower()
    reply = str(decision.get("response") or "").strip()
    cfg = get_google_calendar_store(phone).load()

    if urls and action not in (
        "google_calendar_save_link",
        "google_calendar_configure",
        "google_calendar_list_events",
    ):
        return {
            **decision,
            "action": "google_calendar_save_link",
            "calendar_public_url": urls[0],
            "response": reply or "A guardar o link da sua agenda Google.",
        }

    if cfg.is_configured() and action == "reply":
        if _CALENDAR_VIEW_RE.search(text) and (
            _MISCONFIGURED_REPLY_RE.search(reply)
            or _ECHO_LINK_REPLY_RE.search(reply)
            or "nao configurei" in reply.lower()
        ):
            return {
                **decision,
                "action": "google_calendar_list_events",
                "response": reply or "Vou consultar a sua agenda.",
            }

    if cfg.is_configured() and action == "reply" and _CALENDAR_VIEW_RE.search(text):
        low = reply.lower()
        if any(
            p in low
            for p in (
                "mostrar",
                "consultar",
                "verificar",
                "listar",
                "vou buscar",
                "vou consultar",
            )
        ) and "planta" not in low and "umidade" not in low:
            return {
                **decision,
                "action": "google_calendar_list_events",
                "response": reply,
            }

    return decision
