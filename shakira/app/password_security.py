"""Utilidades de seguranca para senhas no WhatsApp."""

from __future__ import annotations

import re

_CANCEL_RE = re.compile(
    r"^\s*(cancelar|cancela|nao|não|desistir)\s*[.!?]?\s*$",
    re.I,
)


def is_security_cancel(user_text: str) -> bool:
    return bool(_CANCEL_RE.match((user_text or "").strip()))
