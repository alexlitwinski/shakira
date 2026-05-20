"""Fluxo multi-passo: link Instagram, descricao opcional, fetch Apify em paralelo."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.instagram_links_parser import (
    InstagramParseError,
    extract_instagram_urls,
    extract_note_without_urls,
    parse_instagram_url,
)
from app.instagram_links_store import get_instagram_store
from app.instagram_profile_fetcher import enrich_and_notify_instagram_profile
from app.user_memory import get_store
from app.user_memory_cache import invalidate_user_memory_cache
from app.whatsapp_steps import truncate_whatsapp

if TYPE_CHECKING:
    from app.config import AppSettings
    from app.evolution import EvolutionClient

import httpx

log = logging.getLogger(__name__)

PENDING_TIMEOUT_SEC = float(
    __import__("os").environ.get("SHAKIRA_INSTAGRAM_PENDING_TIMEOUT_SEC", "1800")
)

_YES_RE = re.compile(r"^\s*(sim|s|quero|com\s+descri[cç][aã]o|1)\s*\.?\s*$", re.I)
_NO_RE = re.compile(r"^\s*(n[aã]o|nao|n|sem\s+descri[cç][aã]o|pular|2)\s*\.?\s*$", re.I)
_CANCEL_RE = re.compile(r"^\s*(cancela|cancelar|esquece|descarta)\b", re.I)
_MIN_DESCRIPTION_LEN = 8


@dataclass
class PendingInstagramLink:
    entry_id: str
    handle: str
    stage: str  # ask_description | collect_description
    created_at: float


_pending: dict[str, PendingInstagramLink] = {}
_fetch_tasks: dict[str, asyncio.Task[None]] = {}


def is_instagram_link_pending(phone: str) -> bool:
    return phone in _pending


def _handle_display(handle: str) -> str:
    h = (handle or "").strip()
    if h.startswith("ig_") or h == "unknown":
        return "perfil Instagram"
    return f"@{h}"


def _ask_description_message(handle: str) -> str:
    disp = _handle_display(handle)
    return (
        f"Recebi o link de {disp}.\n\n"
        "Quer adicionar uma descricao? Responda *sim* (e envie o texto na proxima mensagem) "
        "ou *nao* para guardar sem descricao."
    )


def _confirm_saved(handle: str, note: str) -> str:
    disp = _handle_display(handle)
    if note.strip():
        short = note if len(note) <= 120 else note[:117] + "..."
        return f"Guardei {disp} com a descricao: «{short}»."
    return f"Guardei {disp} no seu registro Instagram (sem descricao)."


async def _send_text(
    *,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    phone: str,
    text: str,
) -> None:
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=truncate_whatsapp(text),
    )


def _start_fetch_task(
    *,
    entry_id: str,
    phone: str,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> None:
    old = _fetch_tasks.get(phone)
    if old and not old.done():
        old.cancel()

    async def _run() -> None:
        try:
            await enrich_and_notify_instagram_profile(
                entry_id=entry_id,
                phone=phone,
                settings=settings,
                evo=evo,
                http=http,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fetch Instagram falhou phone=%s entry=%s", phone, entry_id)

    _fetch_tasks[phone] = asyncio.create_task(_run())


def _clear_pending(phone: str) -> None:
    _pending.pop(phone, None)


async def _finalize_save(
    phone: str,
    entry_id: str,
    note: str,
    *,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> str:
    store = get_instagram_store(phone)
    ent = store.mark_saved(entry_id, user_note=note)
    _clear_pending(phone)
    if not ent:
        return "Nao encontrei o registro para guardar."
    invalidate_user_memory_cache(get_store(phone))
    msg = _confirm_saved(ent.handle, note)
    await _send_text(
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        phone=phone,
        text=msg,
    )
    return msg


async def _begin_instagram_link_flow(
    phone: str,
    *,
    url: str,
    user_note: str,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    try:
        parsed = parse_instagram_url(url)
    except InstagramParseError as e:
        await _send_text(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
            text=f"Link Instagram invalido: {e}",
        )
        return True

    handle = parsed.handle or "unknown"
    store = get_instagram_store(phone)

    existing = store.find_by_handle(handle, saved_only=True) if handle != "unknown" else None
    if existing and user_note.strip():
        existing.user_note = user_note.strip()[:2000]
        existing.save_status = "saved"
        store.update_entry(existing)
        invalidate_user_memory_cache(get_store(phone))
        await _send_text(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
            text=_confirm_saved(existing.handle, existing.user_note),
        )
        _start_fetch_task(
            entry_id=existing.id,
            phone=phone,
            settings=settings,
            evo=evo,
            http=http,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    entry = store.create_draft(url=parsed.canonical_url, handle=handle, user_note="")
    _start_fetch_task(
        entry_id=entry.id,
        phone=phone,
        settings=settings,
        evo=evo,
        http=http,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
    )

    if user_note.strip():
        await _finalize_save(
            phone,
            entry.id,
            user_note,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    _pending[phone] = PendingInstagramLink(
        entry_id=entry.id,
        handle=entry.handle,
        stage="ask_description",
        created_at=time.monotonic(),
    )
    await _send_text(
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        phone=phone,
        text=_ask_description_message(entry.handle),
    )
    return True


async def try_handle_instagram_link_pending(
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
    pending = _pending.get(phone)
    if not pending:
        return False

    if time.monotonic() - pending.created_at > PENDING_TIMEOUT_SEC:
        await _finalize_save(
            phone,
            pending.entry_id,
            "",
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    t = (text or "").strip()
    if not t:
        return False

    if _CANCEL_RE.search(t):
        store = get_instagram_store(phone)
        store.delete_entry(pending.entry_id)
        _clear_pending(phone)
        await _send_text(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
            text="Cancelado. O link nao foi guardado.",
        )
        return True

    if pending.stage == "ask_description":
        if _YES_RE.match(t):
            pending.stage = "collect_description"
            await _send_text(
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
                phone=phone,
                text="Envie a descricao que quer associar a este perfil.",
            )
            return True
        if _NO_RE.match(t):
            await _finalize_save(
                phone,
                pending.entry_id,
                "",
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
            return True
        if len(t) >= _MIN_DESCRIPTION_LEN and not _YES_RE.match(t) and not _NO_RE.match(t):
            await _finalize_save(
                phone,
                pending.entry_id,
                t,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
            return True
        await _send_text(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
            text="Responda *sim* ou *nao*, ou envie a descricao diretamente.",
        )
        return True

    if pending.stage == "collect_description":
        if _NO_RE.match(t):
            await _finalize_save(
                phone,
                pending.entry_id,
                "",
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
            return True
        if len(t) >= 2:
            await _finalize_save(
                phone,
                pending.entry_id,
                t,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
            return True
        await _send_text(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
            text="Envie um texto para a descricao ou *nao* para guardar sem.",
        )
        return True

    return False


async def try_handle_instagram_link_inbound(
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
    if is_instagram_link_pending(phone):
        return False

    urls = extract_instagram_urls(text or "")
    if not urls:
        return False

    note = extract_note_without_urls(text or "", urls)
    return await _begin_instagram_link_flow(
        phone,
        url=urls[0],
        user_note=note,
        settings=settings,
        evo=evo,
        http=http,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
    )
