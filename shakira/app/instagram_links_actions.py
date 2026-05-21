"""Listagem, apagar e reenviar perfis Instagram guardados."""

from __future__ import annotations

import asyncio
import re
from typing import Any, TYPE_CHECKING

from app.instagram_links_store import InstagramLinkEntry, get_instagram_store
from app.instagram_profile_fetcher import format_profile_summary
from app.user_memory_cache import invalidate_user_memory_cache
from app.user_memory import get_store

if TYPE_CHECKING:
    from app.config import AppSettings
    from app.evolution import EvolutionClient

import httpx

REGISTRY_LIST_DISPLAY_LIMIT = 20

_LIST_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:listar|mostrar|mostre|mostra|quais|ver)\b.*\b(?:"
    r"instagram|perfis?\s+instagram|links?\s+instagram"
    r")\b"
    r"|"
    r"\b(?:instagram|perfis?\s+instagram)\b.*\b(?:"
    r"guardad|listar|itens|tenho|mem[oó]ria"
    r")\b"
    r"|"
    r"\bperfis?\s+(?:do\s+)?instagram\b.*\b(?:mem[oó]ria|guardad|tenho)"
    r")",
    re.IGNORECASE,
)

_SEARCH_PROFILE_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:quero|preciso|busco|achar|encontrar|tem|tenho|qual|quais)\b.*\b(?:perfil|perfis)\b"
    r"|"
    r"\b(?:perfil|perfis)\b.*\b(?:sobre|fala|fale|relacionad|mencion|trata|falando)"
    r"|"
    r"\b(?:perfil|perfis)\b.*\b(?:instagram|guardad|mem[oó]ria)"
    r")",
    re.IGNORECASE,
)

_PHOTO_SEARCH_WORDS = re.compile(
    r"\b(?:foto|fotos|imagem|imagens|photoprism|acervo|galeria|album|[áa]lbum)\b",
    re.IGNORECASE,
)

_REFRESH_INTENT_RE = re.compile(
    r"\b(?:atualiz|atualize|refresh|renov|busca\s+de\s+novo|atualizar)\b",
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


def format_instagram_links_list(phone: str, *, for_user: bool = True) -> str:
    saved = get_instagram_store(phone).list_saved()
    if not saved:
        return "Voce ainda nao tem perfis Instagram guardados."
    recent = saved[-REGISTRY_LIST_DISPLAY_LIMIT:]
    lines = ["Perfis Instagram guardados (mais recentes):"]
    start = len(saved) - len(recent) + 1
    for i, e in enumerate(recent, start=start):
        note = f" — {e.user_note}" if e.user_note else ""
        bio_bit = ""
        if e.profile_bio and not note:
            b = e.profile_bio.replace("\n", " ")
            bio_bit = f" — {b[:60]}..." if len(b) > 60 else f" — {b}"
        lines.append(f"{i}. @{e.handle}{note}{bio_bit}")
    extra = len(saved) - len(recent)
    if extra > 0:
        lines.append(f"(+ {extra} mais antigos)")
    return "\n".join(lines)


def _normalize_search_text(text: str) -> str:
    return (text or "").casefold()


def _entry_search_blob(entry: InstagramLinkEntry) -> str:
    parts = [
        entry.handle,
        entry.user_note,
        entry.profile_name,
        entry.profile_bio,
    ]
    return _normalize_search_text(" ".join(p for p in parts if p))


def search_instagram_profiles(phone: str, query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Diga sobre o que quer buscar nos perfis guardados (ex.: IA, medicina)."
    saved = get_instagram_store(phone).list_saved()
    if not saved:
        return "Voce ainda nao tem perfis Instagram guardados."

    tokens = [t for t in re.split(r"\W+", _normalize_search_text(q)) if len(t) >= 2]
    stop = frozenset(
        {
            "quero",
            "preciso",
            "busco",
            "achar",
            "encontrar",
            "tenho",
            "tem",
            "qual",
            "quais",
            "um",
            "uma",
            "uns",
            "umas",
            "perfil",
            "perfis",
            "que",
            "fale",
            "fala",
            "falam",
            "sobre",
            "com",
            "de",
            "do",
            "da",
            "dos",
            "das",
            "no",
            "na",
            "nos",
            "nas",
            "meu",
            "minha",
            "meus",
            "minhas",
        }
    )
    tokens = [t for t in tokens if t not in stop]
    if not tokens:
        tokens = [t for t in re.split(r"\W+", _normalize_search_text(q)) if len(t) >= 2]

    scored: list[tuple[int, InstagramLinkEntry]] = []
    for entry in saved:
        blob = _entry_search_blob(entry)
        score = sum(1 for tok in tokens if tok in blob)
        if score > 0:
            scored.append((score, entry))

    if not scored:
        return (
            f"Nao encontrei perfis guardados relacionados a «{q}». "
            "Tente outras palavras ou peca a lista completa."
        )

    scored.sort(key=lambda x: (-x[0], x[1].saved_at or x[1].created_at))
    lines = [f"Perfis que combinam com «{q}»:"]
    for _score, e in scored[:10]:
        note = f" — {e.user_note}" if e.user_note else ""
        bio = ""
        if e.profile_bio:
            b = e.profile_bio.replace("\n", " ")
            bio = f"\n   Bio: {b[:120]}" if len(b) > 120 else f"\n   Bio: {b}"
        lines.append(f"• @{e.handle}{note}{bio}")
    return "\n".join(lines)


def is_search_instagram_profiles_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t or _PHOTO_SEARCH_WORDS.search(t):
        return False
    return bool(_SEARCH_PROFILE_INTENT_RE.search(t))


def try_search_instagram_profiles_reply(phone: str, text: str) -> str | None:
    if not is_search_instagram_profiles_intent(text):
        return None
    return search_instagram_profiles(phone, text)


def handle_search_instagram_links(decision: dict[str, Any], phone: str) -> str:
    query = str(decision.get("instagram_search_query") or "").strip()
    if not query:
        query = str(decision.get("response") or "").strip()
    return search_instagram_profiles(phone, query)


async def try_handle_refresh_instagram_inbound(
    phone: str,
    text: str,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    if not is_refresh_instagram_profile_intent(text):
        return False
    handle = extract_refresh_handle(text)
    ent = resolve_entry_for_reference(phone, handle=handle) if handle else None
    if not ent:
        saved = get_instagram_store(phone).list_saved()
        if len(saved) == 1 and not handle:
            ent = saved[0]
    if not ent:
        return False

    from app.instagram_profile_fetcher import enrich_and_notify_instagram_profile

    store = get_instagram_store(phone)
    ent.fetch_status = "pending"
    store.update_entry(ent)
    invalidate_user_memory_cache(get_store(phone))

    asyncio.create_task(
        enrich_and_notify_instagram_profile(
            entry_id=ent.id,
            phone=phone,
            settings=settings,
            evo=evo,
            http=http,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
    )
    msg = f"A atualizar @{ent.handle}. Envio o resumo atualizado em instantes."
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=msg,
    )
    return True


def is_refresh_instagram_profile_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t or not _REFRESH_INTENT_RE.search(t):
        return False
    low = t.casefold()
    return "instagram" in low or "perfil" in low or "@" in t


def extract_refresh_handle(text: str) -> str:
    m = re.search(r"@([a-z0-9._]{1,30})", text, re.I)
    if m:
        return m.group(1).lower()
    return ""


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
