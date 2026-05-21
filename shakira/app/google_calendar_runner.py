"""Executor periodico: alertas de eventos e resumo diario Google Calendar."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import AppSettings
from app.conversation_history import append as history_append
from app.evolution import EvolutionClient
from app.google_calendar_parser import fetch_calendar_events, format_event_time, format_events_list
from app.google_calendar_store import GoogleCalendarConfig, GoogleCalendarStore, iter_configured_calendar_stores
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TICK_SECONDS = 60
_ICS_CACHE_SEC = 300
_MAX_SENT_ALERT_KEYS = 200

_runner_instance: "GoogleCalendarRunner | None" = None


def set_google_calendar_runner(runner: "GoogleCalendarRunner | None") -> None:
    global _runner_instance
    _runner_instance = runner


def ensure_google_calendar_runner_running() -> None:
    if _runner_instance:
        _runner_instance.ensure_running()


@dataclass
class _IcsCacheEntry:
    fetched_at: float
    events: list[Any]


@dataclass
class GoogleCalendarRunner:
    settings: AppSettings
    evo: EvolutionClient
    http: httpx.AsyncClient
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _ics_cache: dict[str, _IcsCacheEntry] = field(default_factory=dict)

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shakira-google-calendar")
        log.info("Executor Google Calendar iniciado (tick=%ss)", TICK_SECONDS)

    def ensure_running(self) -> None:
        if not self.is_running():
            self.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("Google Calendar runner tick falhou")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _get_events(
        self,
        phone: str,
        cfg: GoogleCalendarConfig,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Any]:
        now_mono = time.monotonic()
        cached = self._ics_cache.get(phone)
        if cached and now_mono - cached.fetched_at < _ICS_CACHE_SEC:
            events = cached.events
        else:
            events = await fetch_calendar_events(
                self.http,
                ics_url=cfg.ics_url,
                tz_name=cfg.timezone,
            )
            self._ics_cache[phone] = _IcsCacheEntry(fetched_at=now_mono, events=events)

        tz = ZoneInfo(cfg.timezone)
        ws = window_start.astimezone(tz)
        we = window_end.astimezone(tz)
        out = []
        for ev in events:
            start = ev.start.astimezone(tz)
            end = ev.end.astimezone(tz) if ev.end else start + timedelta(hours=1)
            if end < ws or start > we:
                continue
            out.append(ev)
        return out

    async def _send(self, phone: str, text: str) -> None:
        if not text.strip():
            return
        try:
            await send_whatsapp_text(
                settings=self.settings,
                evo=self.evo,
                number=phone,
                message=text,
            )
            history_append(phone, "[auto-agenda]", text)
        except WhatsAppSendError as e:
            log.warning("Google Calendar WhatsApp falhou phone=%s: %s", phone, e)

    async def _tick(self) -> None:
        for phone, store, cfg in iter_configured_calendar_stores():
            tz = ZoneInfo(cfg.timezone)
            now = datetime.now(tz)
            await self._maybe_daily_summary(phone, store, cfg, now, tz)
            await self._maybe_event_alerts(phone, store, cfg, now, tz)

    async def _maybe_daily_summary(
        self,
        phone: str,
        store: GoogleCalendarStore,
        cfg: GoogleCalendarConfig,
        now: datetime,
        tz: ZoneInfo,
    ) -> None:
        if not cfg.daily_summary_enabled:
            return
        hh, mm = cfg.daily_summary_time.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if abs((now - target).total_seconds()) > TICK_SECONDS:
            return
        today = now.date().isoformat()
        if cfg.last_daily_summary_date == today:
            return

        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=1)
        try:
            events = await self._get_events(
                phone,
                cfg,
                window_start=window_start,
                window_end=window_end,
            )
        except Exception:
            log.exception("Resumo diario falhou phone=%s", phone)
            return

        title = f"Resumo do dia — {now.strftime('%d/%m/%Y')}"
        msg = format_events_list(events, title=title, tz_name=cfg.timezone)
        await self._send(phone, msg)
        cfg.last_daily_summary_date = today
        store.save(cfg)

    async def _maybe_event_alerts(
        self,
        phone: str,
        store: GoogleCalendarStore,
        cfg: GoogleCalendarConfig,
        now: datetime,
        tz: ZoneInfo,
    ) -> None:
        if not cfg.alerts_enabled or cfg.alert_advance_minutes <= 0:
            return

        horizon = now + timedelta(hours=26)
        try:
            events = await self._get_events(
                phone,
                cfg,
                window_start=now - timedelta(minutes=5),
                window_end=horizon,
            )
        except Exception:
            log.exception("Alertas agenda falhou phone=%s", phone)
            return

        changed = False
        for ev in events:
            start = ev.start.astimezone(tz)
            alert_at = start - timedelta(minutes=cfg.alert_advance_minutes)
            if now < alert_at or now > start + timedelta(minutes=5):
                continue
            key = f"{ev.uid}|{start.isoformat()}"
            if key in cfg.sent_event_alerts:
                continue

            when = format_event_time(ev, tz=tz)
            lines = [
                f"Lembrete de agenda ({cfg.alert_advance_minutes} min):",
                "",
                f"• {when} — {ev.summary}",
            ]
            if ev.location:
                lines.append(f"Local: {ev.location}")
            await self._send(phone, "\n".join(lines))
            cfg.sent_event_alerts[key] = now.isoformat()
            changed = True

        if changed:
            if len(cfg.sent_event_alerts) > _MAX_SENT_ALERT_KEYS:
                items = sorted(cfg.sent_event_alerts.items(), key=lambda x: x[1])[-_MAX_SENT_ALERT_KEYS:]
                cfg.sent_event_alerts = dict(items)
            store.save(cfg)
