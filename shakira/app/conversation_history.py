"""Historico recente de mensagens WhatsApp por telefone (em memoria)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

MAX_MESSAGES = 20


@dataclass(frozen=True)
class HistoryEntry:
    role: str  # "user" | "assistant"
    text: str


_histories: dict[str, deque[HistoryEntry]] = {}


def get_recent(phone: str) -> list[HistoryEntry]:
    dq = _histories.get(phone)
    if not dq:
        return []
    return list(dq)


def append(phone: str, role: str, text: str) -> None:
    t = text.strip()
    if not t or role not in ("user", "assistant"):
        return
    if phone not in _histories:
        _histories[phone] = deque(maxlen=MAX_MESSAGES)
    _histories[phone].append(HistoryEntry(role=role, text=t))


def record_exchange(phone: str, user_text: str, assistant_text: str) -> None:
    append(phone, "user", user_text)
    append(phone, "assistant", assistant_text)


def format_for_prompt(entries: list[HistoryEntry]) -> str:
    if not entries:
        return ""
    lines: list[str] = []
    for entry in entries:
        label = "Usuario" if entry.role == "user" else "Assistente"
        lines.append(f"{label}: {entry.text}")
    return (
        "Historico recente da conversa (ultimas mensagens, do mais antigo ao mais recente):\n"
        + "\n".join(lines)
    )
