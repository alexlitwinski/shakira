"""Fluxo de arquivo recebido sem instrucao: perguntar destino (memoria pessoal vs PhotoPrism)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.user_memory import InboundContent, UserMemoryStore

if TYPE_CHECKING:
    from app.config import AppSettings
    from app.evolution import EvolutionClient

_PLACEHOLDER_PREFIX = "[usuario enviou"

_SAVE_WORDS = ("guarda", "salva", "salvar", "lembr", "anot", "arquiva", "arquivo")
_PERSONAL_WORDS = (
    "pessoal",
    "memoria",
    "memória",
    "registro",
    "convite",
    "meu arquivo",
    "minha memoria",
    "minha memória",
    "guardar aqui",
    "guarda aqui",
    "1",
    "um",
    "opcao 1",
    "opção 1",
)
_PHOTOPRISM_WORDS = (
    "photoprism",
    "photo prism",
    "acervo",
    "galeria",
    "biblioteca de fotos",
    "fotos da casa",
    "2",
    "dois",
    "opcao 2",
    "opção 2",
)
_CANCEL_WORDS = ("cancela", "cancelar", "esquece", "descarta", "nao quero", "não quero", "deixa")


def is_placeholder_user_text(text: str) -> bool:
    return text.strip().startswith(_PLACEHOLDER_PREFIX)


def _combined_intent_text(inbound: InboundContent) -> str:
    parts: list[str] = []
    if inbound.media and inbound.media.caption:
        parts.append(inbound.media.caption)
    if not is_placeholder_user_text(inbound.text):
        parts.append(inbound.text)
    return " ".join(parts).strip()


def media_has_explicit_intent(inbound: InboundContent) -> bool:
    """True se legenda ou texto ja indicam guardar (pessoal ou PhotoPrism)."""
    combined = _combined_intent_text(inbound).casefold()
    if not combined:
        return False
    if any(w in combined for w in _SAVE_WORDS):
        return True
    if any(w in combined for w in _PHOTOPRISM_WORDS):
        return True
    return False


def classify_explicit_media_intent(inbound: InboundContent) -> str:
    """photoprism | personal"""
    combined = _combined_intent_text(inbound).casefold()
    if any(w in combined for w in _PHOTOPRISM_WORDS):
        return "photoprism"
    return "personal"


def extract_album_name(text: str) -> str:
    """Extrai nome de album de frases como 'album Viagens' ou 'no album festa'."""
    t = text.strip()
    patterns = [
        r"(?:álbum|album)\s+[\"']?([^\"'\n.]+)[\"']?",
        r"(?:no|na|para o|para a)\s+(?:álbum|album)\s+[\"']?([^\"'\n.]+)[\"']?",
    ]
    for pat in patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if name:
                return name[:120]
    return ""


def classify_pending_reply(text: str, *, is_image: bool) -> str:
    """
    personal | photoprism | cancel | unknown
    """
    lower = text.casefold().strip()
    if not lower:
        return "unknown"
    if any(w in lower for w in _CANCEL_WORDS):
        return "cancel"
    if any(w in lower for w in _PHOTOPRISM_WORDS):
        return "photoprism"
    if any(w in lower for w in _PERSONAL_WORDS) or any(w in lower for w in _SAVE_WORDS):
        return "personal"
    if lower in ("sim", "s", "yes", "ok", "pode"):
        return "personal"
    if is_image and re.search(r"\b(?:álbum|album)\b", lower):
        return "photoprism"
    return "unknown"


def build_media_choice_prompt(*, is_image: bool, filename: str) -> str:
    name = filename or "arquivo"
    if is_image:
        return (
            f"Recebi a foto *{name}*. O que deseja fazer?\n\n"
            "1) *Memória pessoal* — guardo para você recuperar depois "
            "(ex.: convite de show, PDF importante). Responda: *pessoal* ou *guardar*.\n\n"
            "2) *PhotoPrism* — envio para o acervo de fotos da casa. "
            "Responda: *PhotoPrism* e, se quiser, o álbum "
            '(ex.: *PhotoPrism álbum Viagens*).\n\n'
            "Para cancelar: *cancelar*."
        )
    return (
        f"Recebi o arquivo *{name}*. Deseja guardar na sua *memória pessoal* "
        "para recuperar depois (ex.: convite de um show)?\n\n"
        "Responda *sim* ou *guardar* — pode incluir um rótulo "
        '(ex.: *guardar convite do show*).\n\n'
        "Para cancelar: *cancelar*."
    )


def build_pending_clarification(*, is_image: bool) -> str:
    if is_image:
        return (
            "Não entendi. Responda *pessoal* (memória para recuperar depois) "
            "ou *PhotoPrism* (acervo de fotos, opcionalmente com álbum)."
        )
    return (
        "Não entendi. Responda *sim* ou *guardar* para memória pessoal, "
        "ou *cancelar* para descartar."
    )


def extract_personal_label(text: str) -> str:
    """Rotulo opcional a partir da resposta do usuario."""
    m = re.search(
        r"(?:guardar|salvar|mem[oó]ria|pessoal|convite|rotulo|rótulo)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()[:120]
    return ""


async def download_inbound_media_bytes(
    inbound: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    instance: str,
) -> tuple[bytes, str, str] | None:
    if not inbound.media or not inbound.record:
        return None
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        return None
    return await evo.get_media_base64(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        message_payload=inbound.record,
    )
