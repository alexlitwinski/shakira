"""Testes detecao de gravacao de credenciais no cofre."""

from __future__ import annotations

from app.vault_credential_detection import (
    is_vault_save_intent,
    memory_decision_looks_like_vault,
    parse_credential_save,
    vault_fields_from_memory_decision,
)


def test_parse_credential_save_inline():
    label, secret = parse_credential_save("Lembra que a senha do wifi é abc123")
    assert label == "wifi"
    assert secret == "abc123"


def test_lembra_senha_intent():
    assert is_vault_save_intent("Lembra que minha senha do netflix é xyz789")


def test_retrieve_not_save():
    assert not is_vault_save_intent("Qual é a senha do wifi?")


def test_guardar_senha_explicit():
    assert is_vault_save_intent("Quero guardar a senha do email")


def test_memory_decision_redirect():
    decision = {
        "memory_text": "senha do wifi é abc123",
        "memory_label": "",
    }
    assert memory_decision_looks_like_vault(decision)
    label, secret = vault_fields_from_memory_decision(decision)
    assert label == "wifi"
    assert secret == "abc123"


def test_non_credential_memory():
    decision = {"memory_text": "aniversário da Maria é 15 de março", "memory_label": ""}
    assert not memory_decision_looks_like_vault(decision)
