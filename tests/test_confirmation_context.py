"""Testes de confirmacao curta (sim/ok) apos pergunta do assistente."""

from __future__ import annotations

from app.confirmation_context import (
    assistant_asked_confirmation,
    augment_user_message_for_affirmative,
    confirmation_execution_retry_message,
    needs_confirmation_execution_retry,
    reply_promises_action,
)
from app.conversation_history import HistoryEntry


def test_assistant_asked_confirmation_detects_quer_que_eu():
    assert assistant_asked_confirmation(
        "Posso diminuir a intensidade do lustre principal, das arandelas e do abajur. "
        "Quer que eu faça isso?"
    )


def test_reply_promises_action_detects_vou_diminuir():
    assert reply_promises_action(
        {"action": "reply", "response": "Ok, vou diminuir a intensidade do lustre principal."}
    )


def test_needs_confirmation_execution_retry():
    history = [
        HistoryEntry(role="user", text="Reduza a iluminação da sala de estar"),
        HistoryEntry(
            role="assistant",
            text=(
                "Para deixar a iluminação mais relaxante, posso diminuir a intensidade "
                "do lustre principal, das arandelas e do abajur. Quer que eu faça isso?"
            ),
        ),
    ]
    decision = {
        "action": "reply",
        "response": "Ok, vou diminuir a intensidade do lustre principal, das arandelas e do abajur.",
    }
    assert needs_confirmation_execution_retry("sim", history, decision)


def test_needs_confirmation_execution_retry_skips_call_service():
    history = [
        HistoryEntry(role="assistant", text="Quer que eu faça isso?"),
    ]
    decision = {
        "action": "call_service",
        "domain": "light",
        "service": "turn_on",
        "service_data": {"entity_id": "light.lustre_estar", "brightness_pct": 30},
        "response": "Vou ajustar as luzes.",
    }
    assert not needs_confirmation_execution_retry("sim", history, decision)


def test_augment_user_message_for_light_confirmation():
    history = [
        HistoryEntry(
            role="assistant",
            text="Posso diminuir a intensidade das luzes. Quer que eu faça isso?",
        ),
    ]
    out = augment_user_message_for_affirmative("sim", history)
    assert "call_service" in out
    assert "NÃO responda só com action=reply" in out


def test_confirmation_execution_retry_message_quotes_last_assistant():
    last = "Quer que eu diminua as luzes da sala?"
    msg = confirmation_execution_retry_message(last)
    assert last in msg
    assert "call_service" in msg
