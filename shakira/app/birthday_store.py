"""Aniversarios guardados por utilizador."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from app.user_memory import USER_DATA_ROOT, sanitize_phone

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = os.environ.get("SHAKIRA_DEFAULT_TIMEZONE", "America/Sao_Paulo")
DEFAULT_NOTIFY_TIME = os.environ.get("SHAKIRA_BIRTHDAY_NOTIFY_TIME", "08:00")
MAX_ENTRIES = int(os.environ.get("SHAKIRA_MAX_BIRTHDAYS_PER_USER", "200"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_hhmm(raw: str, *, default: str = DEFAULT_NOTIFY_TIME) -> str:
    text = (raw or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return default
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return default
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return default
    return f"{hh:02d}:{mm:02d}"


@dataclass
class BirthdayEntry:
    id: str
    name: str
    month: int
    day: int
    year: int | None = None
    note: str = ""
    created_at: str = field(default_factory=_now_iso)

    def display_date(self) -> str:
        if self.year:
            return f"{self.day:02d}/{self.month:02d}/{self.year}"
        return f"{self.day:02d}/{self.month:02d}"

    def age_on(self, ref: date) -> int | None:
        if not self.year:
            return None
        age = ref.year - self.year
        if (ref.month, ref.day) < (self.month, self.day):
            age -= 1
        return max(0, age)

    def next_occurrence(self, ref: date) -> date:
        """Proxima data de aniversario a partir de ref (ignora ano de nascimento)."""
        try:
            candidate = date(ref.year, self.month, self.day)
        except ValueError:
            # 29/fev em ano nao bissexto -> 28/fev
            candidate = date(ref.year, self.month, 28)
        if candidate < ref:
            try:
                candidate = date(ref.year + 1, self.month, self.day)
            except ValueError:
                candidate = date(ref.year + 1, self.month, 28)
        return candidate

    def days_until(self, ref: date) -> int:
        return (self.next_occurrence(ref) - ref).days


@dataclass
class BirthdayConfig:
    notify_time: str = DEFAULT_NOTIFY_TIME
    timezone: str = DEFAULT_TIMEZONE
    last_weekly_summary_date: str = ""
    last_daily_notified: dict[str, str] = field(default_factory=dict)
    entries: list[BirthdayEntry] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)

    def has_entries(self) -> bool:
        return bool(self.entries)


_store_cache: dict[str, "BirthdayStore"] = {}


class BirthdayStore:
    def __init__(self, phone: str) -> None:
        self.phone = sanitize_phone(phone)
        self.root = USER_DATA_ROOT / self.phone
        self.path = self.root / "birthdays.json"

    def _entry_from_row(self, row: dict[str, Any]) -> BirthdayEntry | None:
        eid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        month = row.get("month")
        day = row.get("day")
        if not eid or not name:
            return None
        if not isinstance(month, int) or not isinstance(day, int):
            return None
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        year = row.get("year")
        yr: int | None = None
        if isinstance(year, int) and 1900 <= year <= 2100:
            yr = year
        elif isinstance(year, str) and year.strip().isdigit():
            y = int(year.strip())
            if 1900 <= y <= 2100:
                yr = y
        return BirthdayEntry(
            id=eid,
            name=name,
            month=month,
            day=day,
            year=yr,
            note=str(row.get("note") or "").strip(),
            created_at=str(row.get("created_at") or _now_iso()),
        )

    def load(self) -> BirthdayConfig:
        if not self.path.is_file():
            return BirthdayConfig()
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("birthdays.json corrompido phone=%s: %s", self.phone, e)
            return BirthdayConfig()
        if not isinstance(row, dict):
            return BirthdayConfig()

        entries: list[BirthdayEntry] = []
        raw_entries = row.get("entries")
        if isinstance(raw_entries, list):
            for item in raw_entries:
                if isinstance(item, dict):
                    ent = self._entry_from_row(item)
                    if ent:
                        entries.append(ent)

        notified: dict[str, str] = {}
        raw_notified = row.get("last_daily_notified")
        if isinstance(raw_notified, dict):
            notified = {str(k): str(v) for k, v in raw_notified.items()}

        return BirthdayConfig(
            notify_time=normalize_hhmm(str(row.get("notify_time") or "")),
            timezone=str(row.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
            last_weekly_summary_date=str(row.get("last_weekly_summary_date") or "").strip(),
            last_daily_notified=notified,
            entries=entries,
            updated_at=str(row.get("updated_at") or _now_iso()),
        )

    def save(self, config: BirthdayConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        config.updated_at = _now_iso()
        payload = {
            "notify_time": config.notify_time,
            "timezone": config.timezone,
            "last_weekly_summary_date": config.last_weekly_summary_date,
            "last_daily_notified": config.last_daily_notified,
            "entries": [asdict(e) for e in config.entries],
            "updated_at": config.updated_at,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_all(self) -> list[BirthdayEntry]:
        return list(self.load().entries)

    def add(
        self,
        name: str,
        month: int,
        day: int,
        *,
        year: int | None = None,
        note: str = "",
    ) -> BirthdayEntry | str:
        name = name.strip()
        if not name:
            return "Informe o nome da pessoa."
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return "Data invalida."
        try:
            date(2000, month, day)
        except ValueError:
            return f"Dia {day} invalido para o mes {month}."

        cfg = self.load()
        if len(cfg.entries) >= MAX_ENTRIES:
            return f"Limite de {MAX_ENTRIES} aniversarios atingido."

        key = name.casefold()
        for existing in cfg.entries:
            if existing.name.casefold() == key and existing.month == month and existing.day == day:
                return f"Ja tenho aniversario de {existing.name} em {existing.display_date()}."

        entry = BirthdayEntry(
            id=str(uuid.uuid4()),
            name=name,
            month=month,
            day=day,
            year=year,
            note=note.strip(),
        )
        cfg.entries.append(entry)
        self.save(cfg)
        return entry

    def delete(self, *, entry_id: str = "", name: str = "", list_number: int = 0) -> str:
        cfg = self.load()
        if not cfg.entries:
            return "Nenhum aniversario guardado."

        target: BirthdayEntry | None = None
        if entry_id:
            target = next((e for e in cfg.entries if e.id == entry_id), None)
        elif list_number >= 1:
            idx = list_number - 1
            if 0 <= idx < len(cfg.entries):
                target = cfg.entries[idx]
        elif name:
            key = name.strip().casefold()
            matches = [e for e in cfg.entries if key in e.name.casefold()]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                lines = [f"Encontrei {len(matches)} aniversarios com esse nome:"]
                for i, e in enumerate(cfg.entries, start=1):
                    if key in e.name.casefold():
                        lines.append(f"{i}. {e.name} — {e.display_date()}")
                lines.append("Diga o numero da lista para apagar.")
                return "\n".join(lines)

        if not target:
            return "Nao encontrei esse aniversario."

        cfg.entries = [e for e in cfg.entries if e.id != target.id]
        cfg.last_daily_notified.pop(target.id, None)
        self.save(cfg)
        return f"Apaguei o aniversario de {target.name} ({target.display_date()})."

    def upcoming(self, days: int = 7, *, ref: date | None = None) -> list[tuple[BirthdayEntry, date, int]]:
        cfg = self.load()
        if ref is None:
            tz = ZoneInfo(cfg.timezone)
            ref = datetime.now(tz).date()
        out: list[tuple[BirthdayEntry, date, int]] = []
        for entry in cfg.entries:
            d = entry.days_until(ref)
            if 0 <= d <= days:
                out.append((entry, entry.next_occurrence(ref), d))
        out.sort(key=lambda x: (x[2], x[0].name.casefold()))
        return out

    def today_birthdays(self, ref: date) -> list[BirthdayEntry]:
        return [e for e in self.load().entries if e.month == ref.month and e.day == ref.day]

    def build_context_text(self) -> str:
        entries = self.list_all()
        if not entries:
            return ""
        parts = ["ANIVERSARIOS GUARDADOS:"]
        for i, e in enumerate(entries, start=1):
            yr = f" nasc={e.year}" if e.year else ""
            note = f' nota="{e.note}"' if e.note else ""
            parts.append(
                f"  {i}. (id={e.id}) {e.name} — {e.display_date()}{yr}{note}"
            )
        parts.append(
            f"Lembretes: resumo semanal (segunda) e aviso no dia, as {self.load().notify_time}."
        )
        return "\n".join(parts)


def get_birthday_store(phone: str) -> BirthdayStore:
    key = sanitize_phone(phone)
    cached = _store_cache.get(key)
    if cached is not None:
        return cached
    store = BirthdayStore(key)
    _store_cache[key] = store
    return store


def iter_stores_with_birthdays() -> Iterator[tuple[str, BirthdayStore, BirthdayConfig]]:
    if not USER_DATA_ROOT.is_dir():
        return
    for child in USER_DATA_ROOT.iterdir():
        if not child.is_dir():
            continue
        path = child / "birthdays.json"
        if not path.is_file():
            continue
        store = get_birthday_store(child.name)
        cfg = store.load()
        if cfg.has_entries():
            yield store.phone, store, cfg


def count_stores_with_birthdays() -> int:
    return sum(1 for _ in iter_stores_with_birthdays())
