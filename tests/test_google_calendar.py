"""Testes Google Calendar parser e store."""

from __future__ import annotations

import base64

from app.google_calendar_parser import parse_google_calendar_public_url, parse_ics_events
from app.google_calendar_store import get_google_calendar_store, normalize_hhmm
from zoneinfo import ZoneInfo


def test_parse_cid_link():
    email = "alexandre.litwinski@gmail.com"
    cid = base64.b64encode(email.encode()).decode().rstrip("=")
    url = f"https://calendar.google.com/calendar/u/0?cid={cid}"
    cal_id, ics_url, _ = parse_google_calendar_public_url(url)
    assert cal_id == email
    assert "ical" in ics_url
    assert email.replace("@", "%40") in ics_url or email in ics_url


def test_parse_ics_basic():
    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt1
DTSTART:20260521T140000Z
DTEND:20260521T150000Z
SUMMARY:Reuniao teste
END:VEVENT
END:VCALENDAR"""
    events = parse_ics_events(ics, tz=ZoneInfo("America/Sao_Paulo"))
    assert len(events) == 1
    assert events[0].summary == "Reuniao teste"


def test_extract_calendar_urls():
    url = "https://calendar.google.com/calendar/u/0?cid=YWxleGFuZHJlLmxpdHdpbnNraUBnbWFpbC5jb20"
    text = f"Configure: {url}"
    urls = __import__("app.google_calendar_parser", fromlist=["extract_google_calendar_urls"]).extract_google_calendar_urls(text)
    assert len(urls) == 1
    assert urls[0] == url


def test_save_link_routine(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511999999999"
    email = "user@gmail.com"
    cid = base64.b64encode(email.encode()).decode().rstrip("=")
    url = f"https://calendar.google.com/calendar/u/0?cid={cid}"
    from app.google_calendar_routine import save_google_calendar_link

    reply, ok = save_google_calendar_link(phone, url)
    assert ok
    assert "guardada" in reply.lower()
    cfg = get_google_calendar_store(phone).load()
    assert cfg.is_configured()
    assert cfg.calendar_id == email


def test_normalize_hhmm():
    assert normalize_hhmm("7:30") == "07:30"
    assert normalize_hhmm("25:00") == "07:00"


def test_decision_override_list(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511777777777"
    email = "user@gmail.com"
    import base64

    from app.google_calendar_overrides import try_google_calendar_decision_override
    from app.google_calendar_routine import save_google_calendar_link

    cid = base64.b64encode(email.encode()).decode().rstrip("=")
    url = f"https://calendar.google.com/calendar/u/0?cid={cid}"
    save_google_calendar_link(phone, url)

    decision = {
        "action": "reply",
        "response": "Ainda nao configurei o acesso ao seu Google Agenda.",
    }
    fixed = try_google_calendar_decision_override(
        decision,
        phone=phone,
        user_text="Mostre o meu calendario",
    )
    assert fixed.get("action") == "google_calendar_list_events"
