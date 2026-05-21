"""Testes de parse da analise Gemini do interfone."""

from app.interfone_vision import (
    InterfoneVisitorAnalysis,
    _parse_visitor_payload,
    build_interfone_vision_prompt,
)


def test_parse_visitor_json() -> None:
    raw = """{
      "visitor_description": "Homem de camisa azul",
      "visitor_type": "entregador",
      "summary": "Parece ser entregador com pacote."
    }"""
    parsed = _parse_visitor_payload(raw)
    assert parsed is not None
    assert parsed.visitor_type == "entregador"
    assert parsed.whatsapp_summary() == "Parece ser entregador com pacote."


def test_whatsapp_summary_fallback() -> None:
    analysis = InterfoneVisitorAnalysis(visitor_description="Pessoa na porta.")
    assert "Pessoa" in analysis.whatsapp_summary()


def test_prompt_mentions_interfone() -> None:
    prompt = build_interfone_vision_prompt(camera_label="Porta de vidro")
    assert "interfone" in prompt.lower()
    assert "Porta de vidro" in prompt
