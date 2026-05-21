"""Handlers Gemini para Google Calendar (link publico por usuario)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.google_calendar_parser import (
    fetch_calendar_events,
    format_events_list,
)
from app.google_calendar_store import (
    GoogleCalendarConfig,
    get_google_calendar_store,
    normalize_hhmm,
)

log = logging.getLogger(__name__)

_MISSING_LINK_MSG = (
    "Ainda nao tenho o link publico da sua agenda Google.\n\n"
    "No Google Calendar: Configuracoes do calendario > Integrar calendario > "
    "copie o *endereco publico* (link com cid=...) e envie aqui.\n\n"
    "Exemplo:\n"
    "https://calendar.google.com/calendar/u/0?cid=..."
)


def _calendar_public_url(decision: dict[str, Any]) -> str:
    return str(
        decision.get("calendar_public_url") or decision.get("google_calendar_url") or ""
    ).strip()


def _calendar_timezone(decision: dict[str, Any], cfg: GoogleCalendarConfig) -> str:
    tz = str(decision.get("calendar_timezone") or "").strip()
    return tz or cfg.timezone


def _parse_advance_minutes(decision: dict[str, Any], cfg: GoogleCalendarConfig) -> int:
    raw = decision.get("calendar_alert_advance_minutes")
    if raw is None:
        return cfg.alert_advance_minutes
    if isinstance(raw, int):
        return max(0, min(raw, 24 * 60))
    if isinstance(raw, str) and raw.strip().isdigit():
        return max(0, min(int(raw.strip()), 24 * 60))
    return cfg.alert_advance_minutes


def _parse_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "sim", "yes", "on"):
            return True
        if low in ("false", "0", "nao", "não", "no", "off"):
            return False
    return default


def handle_google_calendar_show_settings(phone: str) -> str:
    cfg = get_google_calendar_store(phone).load()
    if not cfg.is_configured():
        return _MISSING_LINK_MSG
    lines = [
        "Configuracao da agenda Google:",
        "",
        f"• Calendario: {cfg.calendar_id or '(link configurado)'}",
        f"• Alertas de eventos: {cfg.alert_advance_minutes} min antes"
        + (" (ativo)" if cfg.alerts_enabled else " (desativado)"),
        f"• Resumo diario: {cfg.daily_summary_time} horario {cfg.timezone}"
        + (" (ativo)" if cfg.daily_summary_enabled else " (desativado)"),
        "",
        "Para alterar, diga por exemplo: "
        "'avise 15 minutos antes' ou 'resumo diario as 8h'.",
    ]
    return "\n".join(lines)


def handle_google_calendar_save_link(
    decision: dict[str, Any],
    phone: str,
) -> str:
    url = _calendar_public_url(decision)
    if not url:
        return (
            str(decision.get("response") or "").strip()
            or "Envie o link publico do Google Calendar (com cid= ou src=)."
        )
    from app.google_calendar_routine import save_google_calendar_link

    base_reply, ok = save_google_calendar_link(phone, url)
    if not ok:
        return base_reply
    confirm = str(decision.get("response") or "").strip()
    if confirm and "http" not in confirm.lower():
        return confirm
    return base_reply


def handle_google_calendar_configure(
    decision: dict[str, Any],
    phone: str,
) -> str:
    store = get_google_calendar_store(phone)
    cfg = store.load()
    if not cfg.is_configured():
        return _MISSING_LINK_MSG

    changed: list[str] = []
    raw_time = decision.get("calendar_daily_summary_time")
    if isinstance(raw_time, str) and raw_time.strip():
        cfg.daily_summary_time = normalize_hhmm(raw_time.strip())
        changed.append(f"resumo diario as {cfg.daily_summary_time}")

    tz = str(decision.get("calendar_timezone") or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
            cfg.timezone = tz
            changed.append(f"fuso {tz}")
        except Exception:
            return f"Fuso horario invalido: {tz}"

    if decision.get("calendar_alert_advance_minutes") is not None:
        cfg.alert_advance_minutes = _parse_advance_minutes(decision, cfg)
        changed.append(f"alertas {cfg.alert_advance_minutes} min antes")

    if decision.get("calendar_alerts_enabled") is not None:
        cfg.alerts_enabled = _parse_bool(decision.get("calendar_alerts_enabled"), cfg.alerts_enabled)
        changed.append("alertas " + ("ativados" if cfg.alerts_enabled else "desativados"))

    if decision.get("calendar_daily_summary_enabled") is not None:
        cfg.daily_summary_enabled = _parse_bool(
            decision.get("calendar_daily_summary_enabled"),
            cfg.daily_summary_enabled,
        )
        changed.append("resumo diario " + ("ativado" if cfg.daily_summary_enabled else "desativado"))

    if not changed:
        return handle_google_calendar_show_settings(phone)

    store.save(cfg)
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    return "Atualizei a agenda: " + ", ".join(changed) + "."


async def handle_google_calendar_list_events(
    decision: dict[str, Any],
    *,
    phone: str,
    http: httpx.AsyncClient,
) -> str:
    store = get_google_calendar_store(phone)
    cfg = store.load()
    if not cfg.is_configured():
        return _MISSING_LINK_MSG

    tz_name = _calendar_timezone(decision, cfg)
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    days = 1
    raw_days = decision.get("calendar_list_days")
    if isinstance(raw_days, int):
        days = max(1, min(raw_days, 14))
    elif isinstance(raw_days, str) and raw_days.strip().isdigit():
        days = max(1, min(int(raw_days.strip()), 14))

    list_date = str(decision.get("calendar_list_date") or "").strip()
    if list_date:
        try:
            start_day = datetime.fromisoformat(list_date).date()
        except ValueError:
            return "Data invalida. Use YYYY-MM-DD."
        window_start = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        window_end = window_start + timedelta(days=1)
        title = f"Agenda — {start_day.strftime('%d/%m/%Y')}"
    else:
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=days)
        if days == 1:
            title = f"Agenda — hoje ({now.strftime('%d/%m/%Y')})"
        else:
            title = f"Agenda — proximos {days} dias"

    try:
        events = await fetch_calendar_events(
            http,
            ics_url=cfg.ics_url,
            tz_name=tz_name,
            window_start=window_start,
            window_end=window_end,
        )
    except httpx.HTTPStatusError:
        return (
            "Nao consegui ler a agenda. Verifique se o calendario esta publico "
            "e se o link ainda e valido."
        )
    except Exception:
        log.exception("Falha ao listar agenda phone=%s", phone)
        return "Nao consegui consultar a agenda agora. Tente de novo em instantes."

    return format_events_list(events, title=title, tz_name=tz_name)
