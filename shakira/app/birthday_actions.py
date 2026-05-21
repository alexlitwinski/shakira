"""Handlers Gemini e formatacao para aniversarios guardados."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.birthday_store import BirthdayEntry, get_birthday_store

_MONTH_NAMES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

_WEEKDAY_NAMES = (
    "segunda-feira",
    "terca-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sabado",
    "domingo",
)


def _parse_int(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def parse_birthday_from_decision(decision: dict[str, Any]) -> tuple[str, int, int, int | None] | str:
    name = str(decision.get("birthday_name") or decision.get("name") or "").strip()
    month = _parse_int(decision.get("birthday_month"))
    day = _parse_int(decision.get("birthday_day"))
    year = _parse_int(decision.get("birthday_year"))

    raw_date = str(decision.get("birthday_date") or "").strip()
    if raw_date and (month is None or day is None):
        parsed = parse_birthday_date_text(raw_date)
        if isinstance(parsed, tuple):
            p_day, p_month, p_year = parsed
            day = day or p_day
            month = month or p_month
            if year is None and p_year:
                year = p_year

    if not name:
        return "Informe o nome da pessoa."
    if month is None or day is None:
        return "Informe a data do aniversario (dia e mes)."
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return "Data invalida."
    if year is not None and not (1900 <= year <= 2100):
        return "Ano de nascimento invalido."
    return name, month, day, year


def parse_birthday_date_text(text: str) -> tuple[int, int, int | None] | str:
    """Parse DD/MM, DD/MM/YYYY ou '15 de marco'."""
    t = (text or "").strip()
    if not t:
        return "Data vazia."

    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?$", t)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        yr_raw = m.group(3)
        year: int | None = None
        if yr_raw:
            year = int(yr_raw)
            if year < 100:
                year += 1900 if year >= 30 else 2000
        return day, month, year

    m = re.match(
        r"^(\d{1,2})\s+de\s+([a-záàâãéêíóôõúç]+)(?:\s+de\s+(\d{4}))?$",
        t,
        re.IGNORECASE,
    )
    if m:
        day = int(m.group(1))
        month_name = m.group(2).casefold().replace("ç", "c")
        month = _MONTH_NAMES.get(month_name)
        if not month:
            return f"Mes desconhecido: {m.group(2)}"
        year = int(m.group(3)) if m.group(3) else None
        return day, month, year

    return f"Nao reconheci a data: {text}"


def format_birthday_entry_line(
    entry: BirthdayEntry,
    *,
    when: date,
    ref: date | None = None,
    include_weekday: bool = False,
    include_relative: bool = False,
) -> str:
    date_str = when.strftime("%d/%m")
    if include_weekday:
        date_str += f" ({_WEEKDAY_NAMES[when.weekday()]})"

    extras: list[str] = []
    if include_relative and ref is not None:
        days = (when - ref).days
        if days == 0:
            extras.append("hoje")
        elif days == 1:
            extras.append("amanha")

    age = entry.age_on(when)
    if age is not None:
        extras.append(f"{age} anos")
    if entry.note:
        extras.append(entry.note)

    line = f"{date_str} — {entry.name}"
    if extras:
        line += f" — {' — '.join(extras)}"
    return line


def format_birthday_line(
    entry: BirthdayEntry,
    *,
    ref: date,
    include_weekday: bool = False,
) -> str:
    when = entry.next_occurrence(ref)
    return format_birthday_entry_line(
        entry,
        when=when,
        ref=ref,
        include_weekday=include_weekday,
        include_relative=True,
    )


def format_birthdays_list(phone: str) -> str:
    store = get_birthday_store(phone)
    items = store.entries_by_proximity()
    if not items:
        return "Voce ainda nao tem aniversarios guardados."
    lines = ["Aniversarios guardados:", ""]
    for entry, when, _ in items:
        lines.append(format_birthday_entry_line(entry, when=when))
    return "\n".join(lines)


def format_upcoming_birthdays(phone: str, days: int = 7) -> str:
    store = get_birthday_store(phone)
    ref = store.reference_date()
    upcoming = store.upcoming(days, ref=ref)
    if not upcoming:
        if days == 7:
            return "Nenhum aniversario nos proximos 7 dias."
        return f"Nenhum aniversario nos proximos {days} dias."

    lines = [f"Aniversarios nos proximos {days} dias:", ""]
    for entry, when, _ in upcoming:
        lines.append(
            format_birthday_entry_line(
                entry,
                when=when,
                ref=ref,
                include_weekday=True,
                include_relative=True,
            )
        )
    return "\n".join(lines)


def handle_birthday_save(decision: dict[str, Any], phone: str) -> str:
    parsed = parse_birthday_from_decision(decision)
    if isinstance(parsed, str):
        return parsed

    name, month, day, year = parsed
    note = str(decision.get("birthday_note") or decision.get("note") or "").strip()
    result = get_birthday_store(phone).add(name, month, day, year=year, note=note)
    if isinstance(result, str):
        return result

    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm

    yr_bit = f" (nascido em {year})" if year else ""
    note_bit = f"\nNota: {note}" if note else ""
    return (
        f"Aniversario de {result.name} guardado: {result.display_date()}{yr_bit}."
        f"{note_bit}\n\n"
        "Vou avisar toda segunda sobre os aniversarios da semana "
        "e no dia do aniversario."
    )


def handle_birthday_list(phone: str) -> str:
    return format_birthdays_list(phone)


def handle_birthday_upcoming(decision: dict[str, Any], phone: str) -> str:
    days = 7
    raw = decision.get("birthday_upcoming_days")
    if isinstance(raw, int):
        days = max(1, min(raw, 30))
    elif isinstance(raw, str) and raw.strip().isdigit():
        days = max(1, min(int(raw.strip()), 30))
    return format_upcoming_birthdays(phone, days)


def handle_birthday_delete(decision: dict[str, Any], phone: str) -> str:
    entry_id = str(decision.get("birthday_id") or "").strip()
    name = str(decision.get("birthday_name") or decision.get("name") or "").strip()
    list_number = _parse_int(decision.get("birthday_list_number")) or 0
    result = get_birthday_store(phone).delete(
        entry_id=entry_id,
        name=name,
        list_number=list_number,
    )
    confirm = str(decision.get("response") or "").strip()
    if confirm and "nao encontrei" not in result.lower():
        return confirm
    return result
