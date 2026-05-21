"""Testes de aniversarios guardados."""

from __future__ import annotations

from datetime import date

from app.birthday_actions import (
    handle_birthday_delete,
    handle_birthday_list,
    handle_birthday_save,
    handle_birthday_upcoming,
    parse_birthday_date_text,
)
from app.birthday_store import get_birthday_store


def test_parse_date_slash():
    assert parse_birthday_date_text("15/03") == (15, 3, None)
    assert parse_birthday_date_text("05/08/1990") == (5, 8, 1990)


def test_parse_date_text_month():
    assert parse_birthday_date_text("20 de dezembro") == (20, 12, None)
    assert parse_birthday_date_text("15 de marco de 1985") == (15, 3, 1985)


def test_save_and_list(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511999999999"

    reply = handle_birthday_save(
        {
            "birthday_name": "Maria",
            "birthday_day": 15,
            "birthday_month": 3,
            "birthday_year": 1990,
        },
        phone,
    )
    assert "Maria" in reply
    assert "guardado" in reply.lower()

    listed = handle_birthday_list(phone)
    assert "Maria" in listed
    assert "15/03" in listed


def test_duplicate_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511888888888"
    decision = {"birthday_name": "Joao", "birthday_day": 1, "birthday_month": 1}
    handle_birthday_save(decision, phone)
    reply = handle_birthday_save(decision, phone)
    assert "Ja tenho" in reply


def test_upcoming(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511777777777"
    ref = date(2026, 5, 19)  # segunda-feira

    store = get_birthday_store(phone)
    store.add("Ana", 19, 5)
    store.add("Bob", 25, 5)

    upcoming = store.upcoming(7, ref=ref)
    names = [e.name for e, _, _ in upcoming]
    assert "Ana" in names
    assert "Bob" in names

    msg = handle_birthday_upcoming({"birthday_upcoming_days": 7}, phone)
    assert "Ana" in msg or "Bob" in msg


def test_delete_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511666666666"
    handle_birthday_save({"birthday_name": "Carla", "birthday_day": 10, "birthday_month": 6}, phone)
    reply = handle_birthday_delete({"birthday_name": "Carla"}, phone)
    assert "Apaguei" in reply
    assert handle_birthday_list(phone) == "Voce ainda nao tem aniversarios guardados."


def test_today_birthdays(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511555555555"
    store = get_birthday_store(phone)
    store.add("Hoje", 21, 5)
    today = date(2026, 5, 21)
    assert len(store.today_birthdays(today)) == 1
    assert store.today_birthdays(today)[0].name == "Hoje"
