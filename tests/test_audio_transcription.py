"""Testes de transcricao de audio."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.audio_transcription import merge_audio_text_with_caption, transcribe_audio_bytes
from app.pending_media import is_inbound_audio, is_storable_file_media
from app.user_memory import InboundContent, InboundMedia


def test_is_inbound_audio() -> None:
    assert is_inbound_audio("audio", "audio/ogg")
    assert is_inbound_audio("document", "audio/mpeg")
    assert not is_inbound_audio("image", "image/jpeg")
    assert not is_storable_file_media("audio", "audio/ogg")
    assert is_storable_file_media("image", "image/jpeg")


def test_merge_caption_and_transcription() -> None:
    assert merge_audio_text_with_caption("Legenda", "Ola casa") == "Legenda\n\nOla casa"
    assert merge_audio_text_with_caption("", "So audio") == "So audio"


@patch("app.audio_transcription.genai.GenerativeModel")
@patch("app.audio_transcription.genai.configure")
def test_transcribe_audio_bytes(mock_configure: MagicMock, mock_model_cls: MagicMock) -> None:
    response = MagicMock()
    response.text = "  Liga a luz da sala  "
    response.candidates = []
    mock_model_cls.return_value.generate_content.return_value = response

    text = transcribe_audio_bytes("key", b"fake-audio", mime_type="audio/ogg")
    assert text == "Liga a luz da sala"


@pytest.mark.asyncio
async def test_resolve_skips_non_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.audio_transcription import resolve_inbound_audio_as_text

    inbound = InboundContent(
        phone="5511999999999",
        text="oi",
        media=InboundMedia(
            mediatype="image",
            filename="a.jpg",
            mimetype="image/jpeg",
            caption="",
            message_record={},
        ),
    )
    resolved, err = await resolve_inbound_audio_as_text(
        inbound,
        settings=MagicMock(gemini_api_key="k", evolution_base_url="", evolution_api_key=""),
        evo=MagicMock(),
        instance="inst",
    )
    assert resolved is None
    assert err is None


@pytest.mark.asyncio
async def test_resolve_audio_when_disabled_returns_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.audio_transcription import resolve_inbound_audio_as_text

    monkeypatch.setenv("SHAKIRA_AUDIO_TRANSCRIPTION_ENABLED", "false")
    inbound = InboundContent(
        phone="5511999999999",
        text="",
        media=InboundMedia(
            mediatype="audio",
            filename="audio.ogg",
            mimetype="audio/ogg",
            caption="",
            message_record={},
        ),
    )
    resolved, err = await resolve_inbound_audio_as_text(
        inbound,
        settings=MagicMock(gemini_api_key="k", evolution_base_url="http://x", evolution_api_key="k"),
        evo=MagicMock(),
        instance="inst",
    )
    assert resolved is None
    assert err and "desativada" in err.casefold()
