"""Testes de formatacao de mensagens de chuva."""

from unittest.mock import patch

from app.rain_message import (
    RainStartStatus,
    build_rain_started_message,
    generate_rain_started_whatsapp,
    short_entity_label,
)


def test_generate_rain_started_whatsapp_uses_gemini():
    status = RainStartStatus(
        open_windows=[],
        porta_vidro_open=False,
        toldo_closed=False,
        toldo_label="Toldo da área gourmet",
    )
    with patch("app.rain_message.genai.GenerativeModel") as mock_model_cls:
        mock_model = mock_model_cls.return_value
        mock_model.generate_content.return_value = type(
            "R", (), {"text": "Começou a chover. Tudo certo por aqui.", "candidates": []}
        )()
        out = generate_rain_started_whatsapp("fake-key", status)
    assert "chover" in out.lower()
    assert "open=" not in out


def test_fallback_without_api_key():
    status = RainStartStatus(open_windows=[], porta_vidro_open=False, toldo_closed=False)
    assert generate_rain_started_whatsapp("", status) == ""
    msg = build_rain_started_message(status)
    assert "Nenhuma janela aberta" in msg
