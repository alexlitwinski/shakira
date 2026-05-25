"""Configuracao Google Calendar por usuario (link publico + alertas)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.user_memory import USER_DATA_ROOT, sanitize_phone

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = os.environ.get("SHAKIRA_DEFAULT_TIMEZONE", "America/Sao_Paulo")
DEFAULT_ALERT_MINUTES = int(os.environ.get("SHAKIRA_CALENDAR_ALERT_MINUTES", "30"))
DEFAULT_SUMMARY_TIME = os.environ.get("SHAKIRA_CALENDAR_SUMMARY_TIME", "07:00")

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_hhmm(raw: str, *, default: str = DEFAULT_SUMMARY_TIME) -> str:
    text = (raw or "").strip()
    m = _TIME_RE.match(text)
    if not m:
        return default
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return default
    return f"{hh:02d}:{mm:02d}"


@dataclass
class GoogleCalendarConfig:
    public_url: str = ""
    calendar_id: str = ""
    ics_url: str = ""
    alert_advance_minutes: int = DEFAULT_ALERT_MINUTES
    daily_summary_time: str = DEFAULT_SUMMARY_TIME
    timezone: str = DEFAULT_TIMEZONE
    alerts_enabled: bool = True
    daily_summary_enabled: bool = True
    sent_event_alerts: dict[str, str] = field(default_factory=dict)
    last_daily_summary_date: str = ""
    updated_at: str = field(default_factory=_now_iso)

    def is_configured(self) -> bool:
        return bool(self.ics_url.strip())

    def summary_line(self) -> str:
        if not self.is_configured():
            return "Agenda Google: link público ainda não configurado."
        parts = [
            f"Agenda Google: link configurado ({self.calendar_id or 'calendário'}).",
            f"Alertas: {self.alert_advance_minutes} min antes"
            + (" (ativo)" if self.alerts_enabled else " (desativado)"),
            f"Resumo diário: {self.daily_summary_time} ({self.timezone})"
            + (" (ativo)" if self.daily_summary_enabled else " (desativado)"),
        ]
        return " ".join(parts)


_store_cache: dict[str, "GoogleCalendarStore"] = {}


class GoogleCalendarStore:
    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.path = self.root / "google_calendar.json"

    def load(self) -> GoogleCalendarConfig:
        if not self.path.is_file():
            return GoogleCalendarConfig()
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("google_calendar.json corrompido phone=%s: %s", self.phone, e)
            return GoogleCalendarConfig()
        if not isinstance(row, dict):
            return GoogleCalendarConfig()
        alerts = row.get("sent_event_alerts")
        sent: dict[str, str] = {}
        if isinstance(alerts, dict):
            sent = {str(k): str(v) for k, v in alerts.items()}
        advance = row.get("alert_advance_minutes")
        adv = DEFAULT_ALERT_MINUTES
        if isinstance(advance, int):
            adv = max(0, min(advance, 24 * 60))
        elif isinstance(advance, str) and advance.strip().isdigit():
            adv = max(0, min(int(advance.strip()), 24 * 60))
        return GoogleCalendarConfig(
            public_url=str(row.get("public_url") or "").strip(),
            calendar_id=str(row.get("calendar_id") or "").strip(),
            ics_url=str(row.get("ics_url") or "").strip(),
            alert_advance_minutes=adv,
            daily_summary_time=normalize_hhmm(str(row.get("daily_summary_time") or "")),
            timezone=str(row.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
            alerts_enabled=bool(row.get("alerts_enabled", True)),
            daily_summary_enabled=bool(row.get("daily_summary_enabled", True)),
            sent_event_alerts=sent,
            last_daily_summary_date=str(row.get("last_daily_summary_date") or "").strip(),
            updated_at=str(row.get("updated_at") or _now_iso()),
        )

    def save(self, config: GoogleCalendarConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        config.updated_at = _now_iso()
        self.path.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def build_context_text(self) -> str:
        cfg = self.load()
        return cfg.summary_line()


def get_google_calendar_store(phone: str) -> GoogleCalendarStore:
    key = sanitize_phone(phone)
    cached = _store_cache.get(key)
    if cached is not None:
        return cached
    store = GoogleCalendarStore(key)
    _store_cache[key] = store
    return store


def iter_configured_calendar_stores() -> Iterator[tuple[str, GoogleCalendarStore, GoogleCalendarConfig]]:
    if not USER_DATA_ROOT.is_dir():
        return
    for child in USER_DATA_ROOT.iterdir():
        if not child.is_dir():
            continue
        path = child / "google_calendar.json"
        if not path.is_file():
            continue
        store = get_google_calendar_store(child.name)
        cfg = store.load()
        if cfg.is_configured():
            yield store.phone, store, cfg


def count_configured_calendars() -> int:
    return sum(1 for _ in iter_configured_calendar_stores())
