"""Fallback: senhas guardadas na memoria pessoal (antes do cofre ou via save_memory)."""

from __future__ import annotations

import re

from app.user_memory import MemoryEntry, get_store

_MEMORY_PASSWORD_RE = re.compile(
    r"\b(?:senha|password|pin|c[oó]digo|credencial|wifi|wi-fi)\b",
    re.I,
)

_LABEL_FROM_TEXT_RE = re.compile(
    r"\b(?:senha|password|pin|c[oó]digo)\s+(?:da|do|de|para)\s+([^,\n.!?]{2,60})",
    re.I,
)


def _entry_blob(entry: MemoryEntry) -> str:
    return f"{entry.label} {entry.text}".casefold()


def is_likely_password_memory(entry: MemoryEntry) -> bool:
    return bool(_MEMORY_PASSWORD_RE.search(_entry_blob(entry)))


def memory_password_display_label(entry: MemoryEntry) -> str:
    if entry.label.strip():
        return entry.label.strip()
    m = _LABEL_FROM_TEXT_RE.search(entry.text)
    if m:
        return m.group(1).strip()
    text = entry.text.strip()
    if len(text) > 50:
        return text[:47] + "..."
    return text


def list_memory_password_hints(phone: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for entry in get_store(phone).list_memories():
        if not is_likely_password_memory(entry):
            continue
        label = memory_password_display_label(entry)
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return labels


def find_memory_password_matches(phone: str, query: str) -> list[MemoryEntry]:
    q = (query or "").strip().casefold()
    out: list[MemoryEntry] = []
    for entry in get_store(phone).list_memories():
        if not is_likely_password_memory(entry):
            continue
        blob = _entry_blob(entry)
        if not q or q in blob:
            out.append(entry)
            continue
        disp = memory_password_display_label(entry).casefold()
        if q in disp:
            out.append(entry)
    return out


def extract_secret_from_memory(entry: MemoryEntry) -> str:
    text = entry.text.strip()
    if not text:
        return ""
    for pattern in (
        r"\b(?:senha|password|pin|c[oó]digo)\s*(?:da|do|de|para)?\s*[^:=\n]{0,40}?\s*(?:é|e|:|=)\s*(.+)$",
        r"\b(?:é|e|:|=)\s*(.+)$",
    ):
        m = re.search(pattern, text, re.I)
        if m:
            secret = m.group(1).strip().strip('"').strip("'")
            if len(secret) >= 2:
                return secret
    return text
