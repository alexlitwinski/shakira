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
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
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
        return "Informe a data do aniversário (dia e mês)."
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return "Data inválida."
    if year is not None and not (1900 <= year <= 2100):
        return "Ano de nascimento inválido."
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
            return f"Mês desconhecido: {m.group(2)}"
        year = int(m.group(3)) if m.group(3) else None
        return day, month, year

    return f"Não reconheci a data: {text}"


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
            extras.append("amanhã")

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
        return "Você ainda não tem aniversários guardados."
    lines = ["Aniversários guardados:", ""]
    for entry, when, _ in items:
        lines.append(format_birthday_entry_line(entry, when=when))
    return "\n".join(lines)


def format_upcoming_birthdays(phone: str, days: int = 7) -> str:
    store = get_birthday_store(phone)
    ref = store.reference_date()
    upcoming = store.upcoming(days, ref=ref)
    if not upcoming:
        if days == 7:
            return "Nenhum aniversário nos próximos 7 dias."
        return f"Nenhum aniversário nos próximos {days} dias."

    lines = [f"Aniversários nos próximos {days} dias:", ""]
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


def execute_birthday_save_batch(decisions: list[dict[str, Any]], phone: str) -> str:
    """Guarda varios aniversarios e devolve um unico texto para WhatsApp."""
    saved: list[str] = []
    errors: list[str] = []
    for decision in decisions:
        reply = handle_birthday_save(decision, phone)
        name = str(decision.get("birthday_name") or "").strip()
        low = reply.lower()
        if any(w in low for w in ("salvo", "guardado", "registrado")):
            if name:
                day = decision.get("birthday_day")
                month = decision.get("birthday_month")
                if isinstance(day, int) and isinstance(month, int):
                    saved.append(f"{name} ({day:02d}/{month:02d})")
                else:
                    saved.append(name)
            else:
                saved.append(reply.strip()[:80])
        else:
            errors.append(reply)

    if errors and not saved:
        return errors[0]

    lines: list[str] = []
    if saved:
        lines.append(
            f"Guardei {len(saved)} aniversário(s):"
            if len(saved) != 1
            else "Aniversário guardado:"
        )
        lines.extend(f"- {line}" for line in saved)
    for err in errors:
        lines.append(err)
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
        f"Aniversário de {result.name} guardado: {result.display_date()}{yr_bit}."
        f"{note_bit}\n\n"
        "Vou avisar toda segunda sobre os aniversários da semana "
        "e no dia do aniversário."
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
