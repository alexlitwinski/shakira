"""Acoes e fallbacks da memoria pessoal (apagar, etc.)."""

from __future__ import annotations

import re
from typing import Any

from app.user_memory import UserMemoryStore, get_store
from app.user_memory_cache import invalidate_user_memory_cache

_DELETE_WORDS = (
    "apaga",
    "apague",
    "exclu",
    "remov",
    "delet",
    "descarta",
    "elimina",
    "tira da memoria",
    "tira da memória",
    "apaga da memoria",
    "apaga da memória",
)

_ID_RE = re.compile(r"\bid[=:\s(]+([a-f0-9]{8,12})\b", re.IGNORECASE)
_FILE_IN_HISTORY_RE = re.compile(
    r"([\w.\-]+\.(?:jpe?g|png|gif|webp|pdf|docx?|xlsx?|txt))\s*\(id",
    re.IGNORECASE,
)


def is_delete_memory_intent(text: str) -> bool:
    lower = text.casefold().strip()
    if not lower:
        return False
    return any(w in lower for w in _DELETE_WORDS)


def _resolve_delete_target(
    store: UserMemoryStore,
    *,
    user_text: str,
    history_text: str,
    file_id: str = "",
    file_name: str = "",
    memory_id: str = "",
) -> tuple[str, str, str]:
    fid = file_id.strip()
    fname = file_name.strip()
    mid = memory_id.strip()

    if fid or fname or mid:
        return fid, fname, mid

    combined = f"{history_text}\n{user_text}"
    m = _ID_RE.search(combined)
    if m:
        return m.group(1), "", ""

    fm = _FILE_IN_HISTORY_RE.search(history_text)
    if fm:
        return "", fm.group(1), ""

    files = store.list_files()
    memories = store.list_memories()
    total = len(files) + len(memories)

    if total == 1:
        if files:
            return files[0].id, "", ""
        return "", "", memories[0].id

    lower = user_text.casefold()
    if total > 0 and re.search(r"\b(ele|ela|isso|este|essa|aquele|aquela)\b", lower):
        if len(files) == 1 and not memories:
            return files[0].id, "", ""
        if len(memories) == 1 and not files:
            return "", "", memories[0].id
        if files:
            return files[-1].id, "", ""

    return "", "", ""


def try_memory_delete_override(
    decision: dict[str, Any],
    *,
    phone: str,
    user_text: str,
    history_text: str,
) -> dict[str, Any]:
    """
    Se o usuario pediu apagar, corrige decisoes erradas (ex.: send_user_file).
    """
    if not is_delete_memory_intent(user_text):
        return decision

    action = str(decision.get("action") or "reply").lower()
    if action == "delete_from_memory":
        return decision

    store = get_store(phone)
    fid, fname, mid = _resolve_delete_target(
        store,
        user_text=user_text,
        history_text=history_text,
        file_id=str(decision.get("file_id") or ""),
        file_name=str(decision.get("file_name") or ""),
        memory_id=str(decision.get("memory_id") or ""),
    )

    if not fid and not fname and not mid:
        return {
            "action": "reply",
            "response": (
                "Nao entendi qual item apagar. Diga o nome do arquivo ou "
                "peca para listar o que esta na sua memoria pessoal."
            ),
        }

    return {
        "action": "delete_from_memory",
        "file_id": fid,
        "file_name": fname,
        "memory_id": mid,
        "response": "Vou apagar da sua memoria pessoal.",
    }


def handle_delete_from_memory(decision: dict[str, Any], phone: str) -> str:
    store = get_store(phone)
    memory_id = str(decision.get("memory_id") or "").strip()
    file_id = str(decision.get("file_id") or "").strip()
    file_name = str(decision.get("file_name") or "").strip()
    confirm = str(decision.get("response") or "").strip()

    if memory_id:
        mems = store.list_memories()
        label = next((m.label or m.text[:40] for m in mems if m.id == memory_id), memory_id)
        if store.delete_memory(memory_id):
            invalidate_user_memory_cache(store)
            return confirm or f"Apaguei a anotacao «{label}» da sua memoria pessoal."
        return "Nao encontrei essa anotacao na sua memoria."

    if file_id or file_name:
        hit = store.find_file(file_id=file_id, filename=file_name)
        if not hit:
            return "Nao encontrei esse arquivo na sua memoria pessoal."
        meta, _path = hit
        display = meta.label or meta.filename
        if store.delete_file(meta.id):
            invalidate_user_memory_cache(store)
            return confirm or f"Apaguei «{display}» da sua memoria pessoal."
        return "Nao consegui apagar o arquivo."

    return confirm or "Nao entendi o que devo apagar da memoria."
