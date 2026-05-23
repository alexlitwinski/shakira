"""Transcricao de audios WhatsApp via Gemini."""

from __future__ import annotations

import logging
import os
import re

import google.generativeai as genai

from app.config import AppSettings
from app.evolution import EvolutionClient
from app.pending_media import download_inbound_media_bytes, is_inbound_audio
from app.user_memory import InboundContent

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = int(os.environ.get("SHAKIRA_MAX_AUDIO_BYTES", str(12 * 1024 * 1024)))

_TRANSCRIPTION_PROMPT = """Transcreva o audio em portugues do Brasil.
Retorne APENAS o texto falado, sem aspas, sem markdown e sem comentarios.
Se nao houver fala inteligivel, retorne uma linha vazia."""


def _normalize_mime(mime_type: str, filename: str) -> str:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("audio/") or mime == "application/ogg":
        return mime if mime != "application/octet-stream" else "audio/ogg"
    name = (filename or "").lower()
    if name.endswith(".ogg"):
        return "audio/ogg"
    if name.endswith(".mp3"):
        return "audio/mpeg"
    if name.endswith(".m4a") or name.endswith(".mp4"):
        return "audio/mp4"
    if name.endswith(".opus"):
        return "audio/opus"
    return "audio/ogg"


def transcribe_audio_bytes(
    api_key: str,
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/ogg",
    filename: str = "",
) -> str:
    """Transcreve bytes de audio; retorna string vazia se falhar ou sem fala."""
    key = (api_key or "").strip()
    if not key or not audio_bytes:
        return ""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        log.warning("Audio muito grande para transcricao: %s bytes", len(audio_bytes))
        return ""

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
    mime = _normalize_mime(mime_type, filename)

    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel(model_name=model_name)
        response = model.generate_content(
            [
                _TRANSCRIPTION_PROMPT,
                {"mime_type": mime, "data": audio_bytes},
            ],
            generation_config=genai.GenerationConfig(temperature=0.1),
        )
    except Exception:
        log.exception("Gemini transcricao de audio falhou")
        return ""

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)
    return (text or "").strip()[:8000]


def merge_audio_text_with_caption(caption: str, transcription: str) -> str:
    cap = (caption or "").strip()
    tx = (transcription or "").strip()
    if cap and tx:
        return f"{cap}\n\n{tx}"
    return tx or cap


def audio_transcription_enabled() -> bool:
    raw = os.environ.get("SHAKIRA_AUDIO_TRANSCRIPTION_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "nao", "não", "off")


async def resolve_inbound_audio_as_text(
    inbound: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    instance: str,
) -> tuple[InboundContent | None, str | None]:
    """
    Se a mensagem for audio, transcreve e devolve InboundContent so com texto.

    Retorna (inbound_atualizado, None) em sucesso.
    (None, mensagem_erro) em falha.
    (None, None) se nao for audio.
    """
    if not inbound.media:
        return None, None

    media = inbound.media
    if not is_inbound_audio(media.mediatype, media.mimetype):
        return None, None

    if not audio_transcription_enabled():
        return None, (
            "Transcricao de mensagens de voz esta desativada. Envie em texto."
        )

    if not (settings.gemini_api_key or "").strip():
        return None, (
            "Para usar mensagens de voz, configure a chave da API Gemini nas opcoes do add-on."
        )

    downloaded = await download_inbound_media_bytes(
        inbound, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return None, "Recebi o audio, mas nao consegui baixa-lo. Tente enviar de novo."

    raw, mimetype, fname = downloaded
    transcription = transcribe_audio_bytes(
        settings.gemini_api_key,
        raw,
        mime_type=mimetype or media.mimetype,
        filename=fname or media.filename,
    )
    if not transcription:
        return None, (
            "Nao consegui entender o audio. Pode repetir ou escrever em texto?"
        )

    user_text = merge_audio_text_with_caption(inbound.text, transcription)
    # Remove qualquer marcador de mídia do WhatsApp no início (ex.: [usuario enviou audio], [Usuário enviou mensagem de voz])
    user_text_cleaned = re.sub(r"^\[[^\]]+\]\s*", "", user_text)
    if user_text_cleaned != user_text:
        user_text = user_text_cleaned.strip() or transcription

    log.info(
        "Audio transcrito phone=%s chars=%s",
        inbound.phone,
        len(user_text),
    )
    return (
        InboundContent(
            phone=inbound.phone,
            text=user_text,
            media=None,
            record=inbound.record,
        ),
        None,
    )
