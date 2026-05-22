"""Testes de detecao e override do historico do interfone."""

from app.interfone_overrides import (
    looks_like_interfone_list_request,
    try_interfone_list_decision_override,
)


def test_looks_like_interfone_list_request():
    assert looks_like_interfone_list_request("Mostre o histórico do interfone")
    assert looks_like_interfone_list_request("ultimas chamadas do interfone")
    assert not looks_like_interfone_list_request("abra o portão social")


def test_override_reply_to_interfone_list():
    out = try_interfone_list_decision_override(
        {
            "action": "reply",
            "response": "Não consegui processar agora. Tente de novo em instantes.",
        },
        user_text="Mostre o histórico do interfone",
    )
    assert out["action"] == "interfone_list"
