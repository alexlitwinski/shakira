"""Acoes e fallbacks da memoria pessoal (apagar, etc.)."""

from __future__ import annotations

import re
from typing import Any

from app.conversation_history import HistoryEntry
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
_HEX_ID_RE = re.compile(r"^[a-f0-9]{8,12}$", re.IGNORECASE)
_FILE_IN_HISTORY_RE = re.compile(
    r"([\w.\-]+\.(?:jpe?g|png|gif|webp|pdf|docx?|xlsx?|txt))\s*\(id",
    re.IGNORECASE,
)
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
_LEGENDA_SUFFIX_RE = re.compile(r"\s*\(legenda:\s*.+\)\s*$", re.IGNORECASE)


def is_delete_memory_intent(text: str) -> bool:
    lower = text.casefold().strip()
    if not lower:
        return False
    return any(w in lower for w in _DELETE_WORDS)


def _is_valid_store_id(value: str) -> bool:
    return bool(_HEX_ID_RE.match(value.strip()))


def parse_delete_indices(user_text: str) -> list[int]:
    if not is_delete_memory_intent(user_text):
        return []
    seen: set[int] = set()
    out: list[int] = []
    for raw in re.findall(r"\b(\d+)\b", user_text):
        n = int(raw)
        if 1 <= n <= 99 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def parse_numbered_list(text: str) -> dict[int, str]:
    items: dict[int, str] = {}
    for line in text.splitlines():
        m = _NUMBERED_LINE_RE.match(line.strip())
        if not m:
            continue
        label = _LEGENDA_SUFFIX_RE.sub("", m.group(2)).strip()
        if label:
            items[int(m.group(1))] = label
    return items


def last_assistant_text(
    history_text: str = "",
    *,
    history_entries: list[HistoryEntry] | None = None,
) -> str:
    if history_entries:
        for entry in reversed(history_entries):
            if entry.role == "assistant":
                return entry.text
        return ""

    parts: list[str] = []
    in_assistant = False
    for line in history_text.splitlines():
        if line.startswith("Assistente: "):
            parts = [line[len("Assistente: ") :]]
            in_assistant = True
        elif line.startswith("Usuario: "):
            in_assistant = False
        elif in_assistant and parts:
            parts.append(line)
    return "\n".join(parts)


def _unified_display_items(store: UserMemoryStore) -> list[tuple[str, str, str]]:
    """Ordem unificada (arquivos, depois anotacoes) como listas do assistente."""
    out: list[tuple[str, str, str]] = []
    for f in store.list_files():
        out.append((f.id, f.filename, ""))
    for m in store.list_memories():
        out.append(("", "", m.id))
    return out


def _match_line_to_target(store: UserMemoryStore, line: str) -> tuple[str, str, str]:
    line_cf = line.casefold().strip()
    if not line_cf:
        return "", "", ""

    for meta in reversed(store.list_files()):
        for candidate in (meta.label, meta.caption, meta.filename):
            c = (candidate or "").strip()
            if not c:
                continue
            cf = c.casefold()
            if cf in line_cf or line_cf in cf:
                return meta.id, meta.filename, ""

    for mem in reversed(store.list_memories()):
        label = (mem.label or "").strip()
        if label and label.casefold() in line_cf:
            return "", "", mem.id
        snippet = mem.text[:80].casefold()
        if snippet and (snippet in line_cf or line_cf in snippet):
            return "", "", mem.id

    return "", "", ""


def resolve_all_delete_targets(
    store: UserMemoryStore,
    *,
    user_text: str,
    history_text: str = "",
    history_entries: list[HistoryEntry] | None = None,
    file_id: str = "",
    file_name: str = "",
    memory_id: str = "",
) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add_target(fid: str, fname: str, mid: str) -> None:
        key = fid or mid
        if not key or key in seen:
            return
        seen.add(key)
        targets.append((fid, fname, mid))

    indices = parse_delete_indices(user_text)
    if indices:
        assistant = last_assistant_text(
            history_text, history_entries=history_entries
        )
        numbered = parse_numbered_list(assistant)
        unified = _unified_display_items(store)
        for idx in indices:
            if idx in numbered:
                fid, fname, mid = _match_line_to_target(store, numbered[idx])
                if fid or mid:
                    add_target(fid, fname, mid)
                    continue
            if 1 <= idx <= len(unified):
                fid, fname, mid = unified[idx - 1]
                add_target(fid, fname, mid)
        if targets:
            return targets

    fid = file_id.strip()
    fname = file_name.strip()
    mid = memory_id.strip()

    if fid and _is_valid_store_id(fid):
        add_target(fid, "", "")
    if fname:
        add_target("", fname, "")
    if mid and _is_valid_store_id(mid):
        add_target("", "", mid)

    if targets:
        return targets

    single = _resolve_delete_target(
        store,
        user_text=user_text,
        history_text=history_text,
        file_id=fid,
        file_name=fname,
        memory_id=mid,
    )
    if single != ("", "", ""):
        return [single]
    return []


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

    if fid and _is_valid_store_id(fid):
        return fid, "", ""
    if fname:
        return "", fname, ""
    if mid and _is_valid_store_id(mid):
        return "", "", mid

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


def execute_memory_deletes(
    phone: str, targets: list[tuple[str, str, str]]
) -> str:
    lines: list[str] = []
    for fid, fname, mid in targets:
        lines.append(
            handle_delete_from_memory(
                {
                    "file_id": fid,
                    "file_name": fname,
                    "memory_id": mid,
                },
                phone,
            )
        )
    return "\n".join(line for line in lines if line.strip())


def try_memory_delete_override(
    decision: dict[str, Any],
    *,
    phone: str,
    user_text: str,
    history_text: str,
    history_entries: list[HistoryEntry] | None = None,
) -> dict[str, Any]:
    """
    Se o usuario pediu apagar, corrige decisoes erradas (ex.: send_user_file)
    e resolve indices da ultima listagem (ex.: "apague 1 e 4").
    """
    if not is_delete_memory_intent(user_text):
        return decision

    store = get_store(phone)
    targets = resolve_all_delete_targets(
        store,
        user_text=user_text,
        history_text=history_text,
        history_entries=history_entries,
        file_id=str(decision.get("file_id") or ""),
        file_name=str(decision.get("file_name") or ""),
        memory_id=str(decision.get("memory_id") or ""),
    )

    if len(targets) > 1:
        return {
            "action": "reply",
            "response": execute_memory_deletes(phone, targets),
        }

    if len(targets) == 1:
        fid, fname, mid = targets[0]
        return {
            "action": "delete_from_memory",
            "file_id": fid,
            "file_name": fname,
            "memory_id": mid,
            "response": "Vou apagar da sua memoria pessoal.",
        }

    return {
        "action": "reply",
        "response": (
            "Nao entendi qual item apagar. Diga o numero da lista, o nome do arquivo "
            "ou peca para listar o que esta na sua memoria pessoal."
        ),
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
