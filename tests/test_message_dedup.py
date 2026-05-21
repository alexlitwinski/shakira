"""Testes de deduplicacao de mensagens WhatsApp."""

from app.handlers import _messages_are_redundant, _substantive_reply


def test_substantive_reply_rejects_placeholder():
    assert not _substantive_reply("Vou verificar a temperatura da agua...")
    assert _substantive_reply("A agua esta em 34 graus.")


def test_messages_are_redundant_substring():
    a = "A temperatura da agua do boiler esta em 34.8 graus."
    b = "Temperatura do Boiler: 34.8°C."
    assert _messages_are_redundant(a, b) or _messages_are_redundant(b, a)
