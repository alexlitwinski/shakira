"""Testes de expansao de lote Gemini no handler."""

from __future__ import annotations

from app.handlers import split_gemini_decisions


def test_split_batch_action() -> None:
    decision = {
        "action": "_batch",
        "batch": [
            {"action": "birthday_save", "birthday_name": "A", "birthday_day": 1, "birthday_month": 1},
            {"action": "birthday_save", "birthday_name": "B", "birthday_day": 2, "birthday_month": 2},
        ],
    }
    assert len(split_gemini_decisions(decision)) == 2


def test_split_reply_with_batch_key() -> None:
    """Caso visto em producao apos _ensure_user_friendly (action reply + batch)."""
    decision = {
        "action": "reply",
        "batch": [
            {"action": "birthday_save", "birthday_name": "Nico", "birthday_day": 3, "birthday_month": 1},
            {"action": "birthday_save", "birthday_name": "Belinha", "birthday_day": 17, "birthday_month": 4},
        ],
    }
    assert len(split_gemini_decisions(decision)) == 2
