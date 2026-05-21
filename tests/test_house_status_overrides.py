"""Testes do override house_status."""

from app.house_status_overrides import (
    looks_like_house_status_request,
    try_house_status_decision_override,
)


def test_looks_like_house_status_request():
    assert looks_like_house_status_request("Como está a casa agora")
    assert looks_like_house_status_request("situação da casa")
    assert not looks_like_house_status_request("como está a memória do servidor")


def test_override_reply_to_house_status():
    decision = {
        "action": "reply",
        "response": "No momento, o perímetro externo...",
    }
    out = try_house_status_decision_override(
        decision,
        user_text="Como está a casa agora",
    )
    assert out["action"] == "house_status"
    assert "Vou verificar" in out["response"]


def test_does_not_override_unrelated_reply():
    decision = {"action": "reply", "response": "A temperatura do boiler está em 47 graus."}
    out = try_house_status_decision_override(
        decision,
        user_text="qual a temperatura do boiler",
    )
    assert out["action"] == "reply"
