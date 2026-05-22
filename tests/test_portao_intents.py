"""Testes de detecao portao social vs portao de servico."""

from __future__ import annotations

from app.portao_social_routine import (
    detect_portao_servico_intent,
    detect_portao_social_intent,
)


def test_servico_not_social():
    msg = "Abra o portão de serviço"
    assert detect_portao_servico_intent(msg)
    assert not detect_portao_social_intent(msg)


def test_social_intent():
    assert detect_portao_social_intent("Abrir o portão social")
    assert detect_portao_social_intent("Abra o portão social")
    assert detect_portao_social_intent("Quero entrar em casa")
    assert not detect_portao_social_intent("Abrir o portão de serviço")


def test_generic_portao_is_social():
    assert detect_portao_social_intent("Abra o portão")
    assert not detect_portao_servico_intent("Abra o portão")
