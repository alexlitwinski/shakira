"""Testes fallback cofre -> memoria pessoal."""

from __future__ import annotations

from app.user_memory import get_store
from app.vault_memory_fallback import (
    extract_secret_from_memory,
    find_memory_password_matches,
    list_memory_password_hints,
)


def test_memory_password_list_and_retrieve(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    phone = "5511999999999"
    get_store(phone).add_memory("A senha da casa é 4521", label="casa")

    hints = list_memory_password_hints(phone)
    assert any("casa" in h.casefold() for h in hints)

    matches = find_memory_password_matches(phone, "casa")
    assert len(matches) >= 1
    assert extract_secret_from_memory(matches[0]) == "4521"
