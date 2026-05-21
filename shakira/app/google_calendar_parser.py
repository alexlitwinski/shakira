"""Parse de links publicos Google Calendar e feeds ICS."""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

_ICS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)
_GOOGLE_CAL_HOSTS = frozenset({"calendar.google.com", "www.google.com"})
_CALENDAR_URL_RE = re.compile(
    r"https?://(?:www\.)?calendar\.google\.com/[^\s<>\"']+",
    re.IGNORECASE,
)


@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime
    end: datetime | None = None
    location: str = ""
    description: str = ""


def _pad_base64(raw: str) -> str:
    s = (raw or "").strip()
    pad = (-len(s)) % 4
    return s + ("=" * pad)


def _decode_cid(raw: str) -> str:
    text = unquote((raw or "").strip())
    try:
        decoded = base64.b64decode(_pad_base64(text), validate=False).decode("utf-8")
        if decoded.strip():
            return decoded.strip()
    except (ValueError, UnicodeDecodeError):
        pass
    return text


def calendar_id_to_ics_url(calendar_id: str) -> str:
    cal_id = (calendar_id or "").strip()
    if not cal_id:
        raise ValueError("calendar_id vazio")
    encoded = quote(cal_id, safe="")
    return f"https://calendar.google.com/calendar/ical/{encoded}/public/basic.ics"


def parse_google_calendar_public_url(url: str) -> tuple[str, str, str]:
    """
    Converte link publico Google Calendar em (calendar_id, ics_url, normalized_public_url).
    Aceita cid=, src=, link ical direto ou email/calendar id.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL vazia")

    if re.fullmatch(r"[^/\s]+@[^/\s]+", raw):
        cal_id = raw
        return cal_id, calendar_id_to_ics_url(cal_id), raw

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host and host not in _GOOGLE_CAL_HOSTS:
        raise ValueError("Use um link publico do Google Calendar (calendar.google.com).")

    path = parsed.path or ""
    ical_match = re.search(r"/ical/([^/]+)/public/basic\.ics", path, re.I)
    if ical_match:
        cal_id = unquote(ical_match.group(1))
        ics_url = calendar_id_to_ics_url(cal_id)
        return cal_id, ics_url, raw

    qs = parse_qs(parsed.query)
    for key in ("cid", "src"):
        values = qs.get(key) or []
        if values:
            cal_id = _decode_cid(values[0])
            if not cal_id:
                raise ValueError(f"Parametro {key} invalido no link.")
            ics_url = calendar_id_to_ics_url(cal_id)
            return cal_id, ics_url, raw

    raise ValueError(
        "Nao reconheci o link. Envie o link publico do Google Calendar "
        "(com cid= ou src=) ou o endereco ical .../public/basic.ics."
    )


def extract_google_calendar_urls(text: str) -> list[str]:
    found = _CALENDAR_URL_RE.findall(text or "")
    out: list[str] = []
    for url in found:
        try:
            parse_google_calendar_public_url(url)
            out.append(url)
        except ValueError:
            continue
    return list(dict.fromkeys(out))


def is_google_calendar_url(url: str) -> bool:
    try:
        parse_google_calendar_public_url(url)
        return True
    except ValueError:
        return False


def _unfold_ics_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line.rstrip("\r"))
    return out


def _parse_prop(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return line, {}, ""
    head, value = line.split(":", 1)
    if ";" in head:
        name, *params = head.split(";")
        props: dict[str, str] = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                props[k.upper()] = v
        return name.upper(), props, value
    return head.upper(), {}, value


def _parse_ics_datetime(value: str, *, tz: ZoneInfo) -> datetime:
    v = (value or "").strip()
    if not v:
        raise ValueError("data vazia")
    if len(v) == 8 and v.isdigit():
        return datetime(int(v[:4]), int(v[4:6]), int(v[6:8]), tzinfo=tz)
    if v.endswith("Z"):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    if "T" in v:
        fmt = "%Y%m%dT%H%M%S" if len(v) >= 15 else "%Y%m%dT%H%M"
        naive = datetime.strptime(v[:15] if fmt.endswith("S") else v[:13], fmt)
        return naive.replace(tzinfo=tz)
    raise ValueError(f"formato ICS desconhecido: {v}")


def parse_ics_events(text: str, *, tz: ZoneInfo) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    current: dict[str, Any] | None = None
    for line in _unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if not current:
                continue
            uid = str(current.get("UID") or "").strip() or f"anon-{len(events)}"
            summary = str(current.get("SUMMARY") or "Sem titulo").strip()
            start_raw = current.get("DTSTART")
            if not start_raw:
                current = None
                continue
            try:
                start = _parse_ics_datetime(str(start_raw), tz=tz)
            except ValueError:
                current = None
                continue
            end: datetime | None = None
            if current.get("DTEND"):
                try:
                    end = _parse_ics_datetime(str(current["DTEND"]), tz=tz)
                except ValueError:
                    end = None
            events.append(
                CalendarEvent(
                    uid=uid,
                    summary=summary,
                    start=start,
                    end=end,
                    location=str(current.get("LOCATION") or "").strip(),
                    description=str(current.get("DESCRIPTION") or "").strip(),
                )
            )
            current = None
            continue
        if current is not None:
            name, _props, value = _parse_prop(line)
            if name in ("DTSTART", "DTEND", "SUMMARY", "UID", "LOCATION", "DESCRIPTION"):
                current[name] = value.replace("\\n", "\n").replace("\\,", ",")
    events.sort(key=lambda e: e.start)
    return events


async def fetch_calendar_events(
    http: httpx.AsyncClient,
    *,
    ics_url: str,
    tz_name: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[CalendarEvent]:
    url = (ics_url or "").strip()
    if not url:
        return []
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")
    resp = await http.get(url, timeout=_ICS_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    events = parse_ics_events(resp.text, tz=tz)

    if window_start is None and window_end is None:
        return events

    ws = window_start or datetime.now(tz)
    we = window_end or (ws + timedelta(days=1))
    if ws.tzinfo is None:
        ws = ws.replace(tzinfo=tz)
    if we.tzinfo is None:
        we = we.replace(tzinfo=tz)

    filtered: list[CalendarEvent] = []
    for ev in events:
        start = ev.start.astimezone(tz)
        end = ev.end.astimezone(tz) if ev.end else start + timedelta(hours=1)
        if end < ws or start > we:
            continue
        filtered.append(ev)
    return filtered


def format_event_time(ev: CalendarEvent, *, tz: ZoneInfo) -> str:
    start = ev.start.astimezone(tz)
    if ev.end and ev.end.date() != ev.start.date():
        end = ev.end.astimezone(tz)
        return f"{start.strftime('%d/%m %H:%M')}–{end.strftime('%d/%m %H:%M')}"
    if start.hour == 0 and start.minute == 0 and (not ev.end or (ev.end - ev.start).days >= 1):
        return "Dia inteiro"
    return start.strftime("%H:%M")


def format_events_list(events: list[CalendarEvent], *, title: str, tz_name: str) -> str:
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")
    if not events:
        return f"{title}\n\nNenhum compromisso nesse periodo."
    lines = [title, ""]
    current_day: date | None = None
    for ev in events:
        start = ev.start.astimezone(tz)
        if current_day != start.date():
            current_day = start.date()
            weekday = (
                "segunda",
                "terça",
                "quarta",
                "quinta",
                "sexta",
                "sábado",
                "domingo",
            )[start.weekday()]
            lines.append(f"*{weekday.capitalize()}, {start.strftime('%d/%m/%Y')}*")
        when = format_event_time(ev, tz=tz)
        line = f"• {when} — {ev.summary}"
        if ev.location:
            line += f" ({ev.location})"
        lines.append(line)
    lines.append(f"\n({len(events)} compromisso(s))")
    return "\n".join(lines)
