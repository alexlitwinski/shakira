"""Executor periodico: resumo semanal (segunda) e aviso no dia do aniversario."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.birthday_actions import format_birthday_entry_line
from app.birthday_store import BirthdayConfig, BirthdayStore, iter_stores_with_birthdays
from app.config import AppSettings
from app.conversation_history import append as history_append
from app.evolution import EvolutionClient
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

log = logging.getLogger(__name__)

TICK_SECONDS = 60
_MAX_NOTIFIED_KEYS = 300

_runner_instance: "BirthdayRunner | None" = None


def set_birthday_runner(runner: "BirthdayRunner | None") -> None:
    global _runner_instance
    _runner_instance = runner


def ensure_birthday_runner_running() -> None:
    if _runner_instance:
        _runner_instance.ensure_running()


@dataclass
class BirthdayRunner:
    settings: AppSettings
    evo: EvolutionClient
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shakira-birthdays")
        log.info("Executor de aniversarios iniciado (tick=%ss)", TICK_SECONDS)

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
                log.exception("Birthday runner tick falhou")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

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
            history_append(phone, "[auto-aniversarios]", text)
        except WhatsAppSendError as e:
            log.warning("Birthday WhatsApp falhou phone=%s: %s", phone, e)

    async def _tick(self) -> None:
        for phone, store, cfg in iter_stores_with_birthdays():
            tz = ZoneInfo(cfg.timezone)
            now = datetime.now(tz)
            if not self._is_notify_window(now, cfg):
                continue
            today = now.date()
            await self._maybe_weekly_summary(phone, store, cfg, now, today)
            await self._maybe_daily_birthday(phone, store, cfg, today)

    def _is_notify_window(self, now: datetime, cfg: BirthdayConfig) -> bool:
        hh, mm = cfg.notify_time.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return abs((now - target).total_seconds()) <= TICK_SECONDS

    async def _maybe_weekly_summary(
        self,
        phone: str,
        store: BirthdayStore,
        cfg: BirthdayConfig,
        now: datetime,
        today,
    ) -> None:
        if now.weekday() != 0:
            return
        today_iso = today.isoformat()
        if cfg.last_weekly_summary_date == today_iso:
            return

        upcoming = store.upcoming(7, ref=today)
        if not upcoming:
            cfg.last_weekly_summary_date = today_iso
            store.save(cfg)
            return

        lines = [
            "Aniversarios desta semana:",
            "",
        ]
        for entry, when, _ in upcoming:
            lines.append(
                format_birthday_entry_line(
                    entry,
                    when=when,
                    ref=today,
                    include_weekday=True,
                    include_relative=True,
                )
            )

        await self._send(phone, "\n".join(lines))
        cfg.last_weekly_summary_date = today_iso
        store.save(cfg)

    async def _maybe_daily_birthday(
        self,
        phone: str,
        store: BirthdayStore,
        cfg: BirthdayConfig,
        today,
    ) -> None:
        year_str = str(today.year)
        changed = False
        for entry in store.today_birthdays(today):
            if cfg.last_daily_notified.get(entry.id) == year_str:
                continue

            age = entry.age_on(today)
            age_bit = f" — {age} anos!" if age is not None else "!"
            note = f"\n{entry.note}" if entry.note else ""
            msg = f"Hoje e aniversario de {entry.name}{age_bit}{note}"
            await self._send(phone, msg)
            cfg.last_daily_notified[entry.id] = year_str
            changed = True

        if changed:
            if len(cfg.last_daily_notified) > _MAX_NOTIFIED_KEYS:
                items = list(cfg.last_daily_notified.items())[-_MAX_NOTIFIED_KEYS:]
                cfg.last_daily_notified = dict(items)
            store.save(cfg)
