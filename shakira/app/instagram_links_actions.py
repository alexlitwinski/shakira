"""Listagem, apagar e reenviar perfis Instagram guardados."""

from __future__ import annotations

import re
from typing import Any

from app.instagram_links_store import InstagramLinkEntry, get_instagram_store
from app.instagram_profile_fetcher import format_profile_summary
from app.user_memory_cache import invalidate_user_memory_cache
from app.user_memory import get_store

REGISTRY_LIST_DISPLAY_LIMIT = 20

_LIST_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:listar|mostrar|mostre|mostra|quais|ver)\b.*\b(?:"
    r"instagram|perfis?\s+instagram|links?\s+instagram"
    r")\b"
    r"|"
    r"\b(?:instagram|perfis?\s+instagram)\b.*\b(?:"
    r"guardad|listar|itens|tenho"
    r")\b"
    r")",
    re.IGNORECASE,
)

_DELETE_WORDS = (
    "apaga",
    "apague",
    "exclu",
    "remov",
    "delet",
    "elimina",
)

_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s+")


def is_list_instagram_links_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_LIST_INTENT_RE.search(t))


def format_instagram_links_list(phone: str) -> str:
    saved = get_instagram_store(phone).list_saved()
    if not saved:
        return "Voce ainda nao tem perfis Instagram guardados."
    recent = saved[-REGISTRY_LIST_DISPLAY_LIMIT:]
    lines = ["Perfis Instagram guardados (mais recentes):"]
    start = len(saved) - len(recent) + 1
    for i, e in enumerate(recent, start=start):
        note = f" — {e.user_note}" if e.user_note else ""
        lines.append(f"{i}. @{e.handle}{note} (id={e.id})")
    extra = len(saved) - len(recent)
    if extra > 0:
        lines.append(f"(+ {extra} mais antigos)")
    return "\n".join(lines)


def try_instagram_links_list_reply(phone: str, text: str) -> str | None:
    if not is_list_instagram_links_intent(text):
        return None
    return format_instagram_links_list(phone)


def resolve_entry_for_reference(
    phone: str,
    *,
    link_id: str = "",
    handle: str = "",
    list_number: int | None = None,
) -> InstagramLinkEntry | None:
    store = get_instagram_store(phone)
    if link_id.strip():
        ent = store.get_by_id(link_id.strip())
        if ent and ent.save_status == "saved":
            return ent
    if handle.strip():
        ent = store.find_by_handle(handle, saved_only=True)
        if ent:
            return ent
    if list_number is not None and list_number >= 1:
        saved = store.list_saved()
        idx = list_number - 1
        if 0 <= idx < len(saved):
            return saved[idx]
    return None


def handle_delete_instagram_link(decision: dict[str, Any], phone: str) -> str:
    store = get_instagram_store(phone)
    link_id = str(decision.get("instagram_link_id") or decision.get("social_link_id") or "").strip()
    handle = str(decision.get("instagram_handle") or "").strip()
    raw_num = decision.get("instagram_list_number")
    num: int | None = None
    if isinstance(raw_num, int):
        num = raw_num
    elif isinstance(raw_num, str) and raw_num.strip().isdigit():
        num = int(raw_num.strip())

    ent = resolve_entry_for_reference(phone, link_id=link_id, handle=handle, list_number=num)
    if not ent:
        reply = str(decision.get("response") or "").strip()
        return reply or "Nao encontrei esse perfil Instagram guardado."
    if store.delete_entry(ent.id):
        invalidate_user_memory_cache(get_store(phone))
        return f"Apaguei o perfil @{ent.handle} do registro Instagram."
    return "Nao consegui apagar o perfil."


def build_send_instagram_summary(entry: InstagramLinkEntry) -> str:
    return format_profile_summary(entry)
