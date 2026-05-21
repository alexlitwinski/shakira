"""Testes do parse JSON do Gemini (objeto ou array)."""

from __future__ import annotations

from app.gemini_parse import (
    parse_gemini_decision_payload,
    parse_gemini_response_text,
    wrap_decisions_for_handler,
)


def test_parse_single_object() -> None:
    raw = '{"action":"birthday_save","birthday_name":"Ana","birthday_day":1,"birthday_month":2}'
    decisions = parse_gemini_response_text(raw)
    assert len(decisions) == 1
    assert decisions[0]["birthday_name"] == "Ana"
    wrapped = wrap_decisions_for_handler(decisions)
    assert wrapped.get("action") == "birthday_save"


def test_parse_array_of_birthdays() -> None:
    raw = """[
      {"action":"birthday_save","birthday_name":"Nico","birthday_day":3,"birthday_month":1},
      {"action":"birthday_save","birthday_name":"Belinha","birthday_day":17,"birthday_month":4}
    ]"""
    decisions = parse_gemini_response_text(raw)
    assert len(decisions) == 2
    wrapped = wrap_decisions_for_handler(decisions)
    assert wrapped["action"] == "_batch"
    assert len(wrapped["batch"]) == 2


def test_parse_batch_key() -> None:
    data = {
        "actions": [
            {"action": "reply", "response": "ok"},
            {"action": "birthday_save", "birthday_name": "X", "birthday_day": 1, "birthday_month": 1},
        ]
    }
    decisions = parse_gemini_decision_payload(data)
    assert len(decisions) == 2
