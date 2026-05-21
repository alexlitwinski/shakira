"""Rotina codificada: guardar link publico Google Calendar ao receber URL."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from app.conversation_history import record_exchange
from app.google_calendar_parser import (
    extract_google_calendar_urls,
    fetch_calendar_events,
    format_events_list,
    parse_google_calendar_public_url,
)
from app.google_calendar_store import get_google_calendar_store
from app.user_friendly import polish_user_message
from app.whatsapp_steps import pulse_whatsapp_typing, truncate_whatsapp

if TYPE_CHECKING:
    from app.evolution import EvolutionClient

log = logging.getLogger(__name__)


def save_google_calendar_link(phone: str, url: str) -> tuple[str, bool]:
    """
    Grava link e valida leitura do ICS.
    Retorna (mensagem, sucesso).
    """
    try:
        cal_id, ics_url, normalized = parse_google_calendar_public_url(url)
    except ValueError as e:
        return f"Nao consegui usar esse link: {e}", False

    store = get_google_calendar_store(phone)
    cfg = store.load()
    cfg.public_url = normalized
    cfg.calendar_id = cal_id
    cfg.ics_url = ics_url
    store.save(cfg)
    log.info("Google Calendar link guardado phone=%s cal_id=%s", phone, cal_id)

    lines = [
        f"Agenda Google guardada ({cal_id}).",
        "",
        f"• Alertas: {cfg.alert_advance_minutes} min antes"
        + (" (ativo)" if cfg.alerts_enabled else " (desativado)"),
        f"• Resumo diario: {cfg.daily_summary_time} ({cfg.timezone})"
        + (" (ativo)" if cfg.daily_summary_enabled else " (desativado)"),
        "",
        "Pode pedir para mudar a antecedencia ou o horario do resumo.",
    ]
    return "\n".join(lines), True


async def verify_calendar_feed(
    http: httpx.AsyncClient,
    *,
    phone: str,
    preview_today: bool = True,
) -> str:
    """Valida ICS e opcionalmente acrescenta preview de hoje."""
    store = get_google_calendar_store(phone)
    cfg = store.load()
    if not cfg.is_configured():
        return ""

    tz_name = cfg.timezone
    tz_now = datetime.now(ZoneInfo(tz_name))
    window_start = tz_now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=1)

    try:
        events = await fetch_calendar_events(
            http,
            ics_url=cfg.ics_url,
            tz_name=tz_name,
            window_start=window_start,
            window_end=window_end,
        )
    except httpx.HTTPStatusError as exc:
        log.warning(
            "ICS Google Calendar HTTP %s phone=%s url=%s",
            exc.response.status_code,
            phone,
            cfg.ics_url[:80],
        )
        return (
            "\n\nNao consegui ler os eventos. Confirme que o calendario esta *publico* "
            "em Configuracoes do calendario > Integrar calendario."
        )
    except Exception:
        log.exception("Falha ao validar ICS phone=%s", phone)
        return (
            "\n\nNao consegui validar a leitura da agenda agora. "
            "Verifique se o calendario esta publico."
        )

    if not preview_today:
        return ""

    title = f"Hoje ({tz_now.strftime('%d/%m/%Y')})"
    preview = format_events_list(events, title=title, tz_name=tz_name)
    return f"\n\n{preview}"


async def try_handle_google_calendar_link_inbound(
    phone: str,
    user_text: str,
    *,
    http: httpx.AsyncClient,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    """
    Se a mensagem contem link publico Google Calendar, grava e confirma.
    Retorna True se consumiu a mensagem (nao chamar Gemini).
    """
    urls = extract_google_calendar_urls(user_text)
    if not urls:
        return False

    if not evo_base or not evo_key or not instance:
        log.error("Google Calendar link sem Evolution phone=%s", phone)
        return True

    url = urls[0]
    base_reply, ok = save_google_calendar_link(phone, url)
    if ok:
        try:
            from app.google_calendar_runner import ensure_google_calendar_runner_running

            ensure_google_calendar_runner_running()
        except ImportError:
            pass
        extra = await verify_calendar_feed(http, phone=phone, preview_today=True)
        reply = base_reply + extra
    else:
        reply = base_reply

    reply = truncate_whatsapp(polish_user_message(reply))
    await pulse_whatsapp_typing()
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=reply,
    )
    record_exchange(phone, user_text, reply)
    log.info("Google Calendar link tratado phone=%s ok=%s", phone, ok)
    return True
