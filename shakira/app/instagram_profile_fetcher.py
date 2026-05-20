"""Fetch de perfil Instagram via Apify e notificacao WhatsApp."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.apify_client import map_instagram_profile_row, run_actor_sync_dataset_items
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.instagram_links_store import InstagramLinkEntry, InstagramLinksStore, get_instagram_store
from app.whatsapp_steps import pulse_whatsapp_typing, truncate_whatsapp

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _format_followers(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def format_profile_summary(entry: InstagramLinkEntry) -> str:
    parts: list[str] = [f"@{entry.handle}"]
    if entry.profile_name:
        parts.append(entry.profile_name)
    foll = _format_followers(entry.followers)
    if foll:
        parts.append(f"{foll} seguidores")
    if entry.is_verified:
        parts.append("verificado")
    if entry.profile_bio:
        bio = entry.profile_bio.replace("\n", " ")
        if len(bio) > 280:
            bio = bio[:277] + "..."
        parts.append(f"Bio: {bio}")
    if entry.user_note:
        parts.append(f"Sua nota: {entry.user_note}")
    return " | ".join(parts)


def _apify_input(entry: InstagramLinkEntry) -> dict[str, Any]:
    if entry.handle:
        return {"usernames": [entry.handle]}
    return {"usernames": [entry.url]}


async def fetch_and_update_entry(
    http: httpx.AsyncClient,
    settings: AppSettings,
    store: InstagramLinksStore,
    entry_id: str,
) -> InstagramLinkEntry | None:
    entry = store.get_by_id(entry_id)
    if not entry:
        return None

    token = settings.apify_api_token.strip()
    actor = settings.apify_instagram_actor.strip() or "apify/instagram-profile-scraper"
    if not token or not settings.instagram_links_fetch_enabled:
        entry.fetch_status = "skipped"
        entry.fetch_error = "Apify nao configurado"
        store.update_entry(entry)
        return entry

    try:
        rows = await run_actor_sync_dataset_items(
            http,
            actor=actor,
            token=token,
            run_input=_apify_input(entry),
        )
    except httpx.HTTPStatusError as e:
        entry.fetch_status = "failed"
        entry.fetch_error = f"HTTP {e.response.status_code}"
        store.update_entry(entry)
        return entry
    except Exception as e:
        entry.fetch_status = "failed"
        entry.fetch_error = str(e)[:200]
        store.update_entry(entry)
        log.warning("Apify falhou id=%s: %s", entry_id, e)
        return entry

    if not rows:
        entry.fetch_status = "failed"
        entry.fetch_error = "perfil sem dados"
        store.update_entry(entry)
        return entry

    mapped = map_instagram_profile_row(rows[0])
    if mapped.get("handle"):
        entry.handle = mapped["handle"]
        if "instagram.com/p/" not in entry.url and "/reel" not in entry.url:
            entry.url = f"https://www.instagram.com/{entry.handle}/"
    entry.profile_name = mapped.get("profile_name") or ""
    entry.profile_bio = mapped.get("profile_bio") or ""
    entry.followers = mapped.get("followers")
    entry.is_verified = mapped.get("is_verified")
    avatar_url = mapped.get("avatar_url") or ""

    if avatar_url:
        try:
            resp = await http.get(avatar_url, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            ext = "jpg"
            ct = (resp.headers.get("content-type") or "").lower()
            if "png" in ct:
                ext = "png"
            elif "webp" in ct:
                ext = "webp"
            entry.avatar_filename = store.save_avatar_bytes(entry.id, resp.content, ext=ext)
        except Exception as e:
            log.warning("Download avatar falhou id=%s: %s", entry_id, e)

    from datetime import datetime, timezone

    entry.fetch_status = "ok"
    entry.fetch_error = ""
    entry.fetched_at = datetime.now(timezone.utc).isoformat()
    store.update_entry(entry)
    return entry


async def notify_profile_fetched(
    *,
    entry: InstagramLinkEntry,
    phone: str,
    settings: AppSettings,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    store: InstagramLinksStore,
) -> None:
    if not evo_base or not evo_key or not instance:
        return

    if entry.fetch_status == "failed":
        text = (
            f"Nao consegui obter os dados do perfil "
            f"@{entry.handle or 'Instagram'}. "
            f"O link ficou guardado na mesma."
        )
        if entry.fetch_error:
            text += f" ({entry.fetch_error[:80]})"
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=truncate_whatsapp(text),
        )
        return

    if entry.fetch_status == "skipped":
        return

    caption = truncate_whatsapp(format_profile_summary(entry))
    path = store.avatar_path(entry)
    await pulse_whatsapp_typing()
    if path:
        data = path.read_bytes()
        mime = "image/jpeg"
        if path.suffix.lower() == ".png":
            mime = "image/png"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        await evo.send_image_bytes(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            image_bytes=data,
            filename=path.name,
            caption=caption,
            mimetype=mime,
        )
    else:
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=caption,
        )


async def enrich_and_notify_instagram_profile(
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
    store = get_instagram_store(phone)
    entry = await fetch_and_update_entry(http, settings, store, entry_id)
    if not entry:
        return
    await notify_profile_fetched(
        entry=entry,
        phone=phone,
        settings=settings,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        store=store,
    )
