"""Comparacao de estados HA para alertas e respostas agendadas."""

from __future__ import annotations

import re

_NUMERIC_WHEN_RE = re.compile(r"^(>=|<=|>|<|==?)\s*([\d.]+)$", re.IGNORECASE)


def state_matches(state: str | None, when_state: str) -> bool:
    """Verifica se o estado satisfaz a condicao (igualdade ou comparacao numerica)."""
    if state is None:
        return False
    st = state.strip()
    cond = when_state.strip()
    if not cond:
        return False

    m = _NUMERIC_WHEN_RE.match(cond)
    if m:
        try:
            val = float(st.replace(",", "."))
            threshold = float(m.group(2))
        except ValueError:
            return False
        op = m.group(1)
        if op == ">":
            return val > threshold
        if op == ">=":
            return val >= threshold
        if op == "<":
            return val < threshold
        if op == "<=":
            return val <= threshold
        if op in ("==", "="):
            return val == threshold
        return False

    return st.lower() == cond.lower()
