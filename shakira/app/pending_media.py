"""Fluxo de arquivo recebido sem instrucao: perguntar destino (memoria pessoal vs PhotoPrism)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.user_memory import InboundContent, UserMemoryStore

if TYPE_CHECKING:
    from app.config import AppSettings
    from app.evolution import EvolutionClient

_PLACEHOLDER_PREFIX = "[usuario enviou"

_SAVE_WORDS = ("salva", "salvar", "lembr", "anot", "arquiva", "arquivo")
_SAVE_WORD_RE = re.compile(
    r"\b(guarda|guardar|salva|salvar|lembr|anot|arquiva|arquivo)\b",
    re.IGNORECASE,
)
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

_GENERIC_DESCRIPTIONS = frozenset(
    {
        "",
        "pessoal",
        "sim",
        "s",
        "yes",
        "ok",
        "pode",
        "guardar",
        "salvar",
        "memoria",
        "memória",
        "registro",
        "arquivo",
        "arquivo guardado",
        "pedido pelo usuario",
        "1",
        "um",
        "2",
        "dois",
        "opcao 1",
        "opção 1",
        "opcao 2",
        "opção 2",
        "guardar aqui",
        "guarda aqui",
    }
)

_WHATSAPP_FILENAME_RE = re.compile(
    r"^[A-F0-9]{8,}(?:\.[A-Za-z0-9]+)?$",
    re.IGNORECASE,
)

_DESTINATION_PREFIX_RE = re.compile(
    r"^(?:pessoal|guardar|salvar|mem[oó]ria|registro(?:\s+pessoal)?)\s*",
    re.IGNORECASE,
)


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
    combined = _combined_intent_text(inbound)
    if not combined:
        return False
    lower = combined.casefold()
    if _SAVE_WORD_RE.search(combined):
        return True
    if any(w in lower for w in _SAVE_WORDS):
        return True
    if any(w in lower for w in _PHOTOPRISM_WORDS):
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


def is_gallery_media(mediatype: str, mime_type: str = "") -> bool:
    """Foto ou video elegivel para PhotoPrism."""
    if mediatype in ("image", "video"):
        return True
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return mime.startswith("image/") or mime.startswith("video/")


def pending_gallery_stats(items: list) -> tuple[int, bool, int]:
    """Retorna (total, has_video, gallery_count) a partir de PendingFile."""
    total = len(items)
    gallery = [p for p in items if is_gallery_media(p.mediatype, p.mime_type)]
    has_video = any(
        p.mediatype == "video" or (p.mime_type or "").startswith("video/") for p in gallery
    )
    return total, has_video, len(gallery)


def classify_pending_reply(text: str, *, supports_gallery: bool) -> str:
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
    if supports_gallery and re.search(r"\b(?:álbum|album)\b", lower):
        return "photoprism"
    return "unknown"


def build_media_choice_prompt(
    *,
    total_count: int = 1,
    gallery_count: int = 0,
    has_video: bool = False,
) -> str:
    if gallery_count <= 0:
        return (
            "Recebi um arquivo. Quer guardar no seu *registro pessoal* "
            "para recuperar depois (ex.: ingresso ou convite)?\n\n"
            "Responda *sim* ou *guardar*."
        )

    n = gallery_count
    if has_video and n > 1:
        kind = "fotos e videos"
    elif has_video:
        kind = "video"
    elif n > 1:
        kind = f"{n} fotos"
    else:
        kind = "uma foto"

    return (
        f"Recebi {kind}. Quer guardar no seu *registro pessoal* "
        "(ingressos, convites, documentos) ou na *galeria da casa* (PhotoPrism)?\n\n"
        "Responda *pessoal* ou *galeria*."
    )


def build_pending_append_notice(
    *,
    total_count: int,
    gallery_count: int,
    has_video: bool,
) -> str:
    if gallery_count <= 0:
        return (
            f"Acrescentei mais um arquivo. Total: *{total_count}*. "
            "Responda *sim* ou *guardar* quando terminar."
        )
    if has_video:
        kind = "midias"
    elif gallery_count > 1:
        kind = "fotos"
    else:
        kind = "foto"
    return (
        f"Acrescentei mais uma {kind}. Total na fila: *{total_count}*. "
        "Responda *pessoal* ou *galeria* quando terminar de enviar."
    )


def build_personal_description_prompt() -> str:
    return (
        "Antes de guardar no seu registro pessoal, preciso saber do que se trata.\n\n"
        "Descreva em poucas palavras (ex.: *ingresso do show*, *convite de casamento*)."
    )


def build_pending_progress_message(
    choice: str,
    *,
    album: str = "",
    count: int = 1,
    has_video: bool = False,
) -> str:
    if choice == "photoprism":
        album_name = album.strip()
        if count > 1:
            if has_video:
                base = f"Enviando {count} midias ao PhotoPrism"
            else:
                base = f"Enviando {count} fotos ao PhotoPrism"
        elif has_video:
            base = "Enviando o video ao PhotoPrism"
        else:
            base = "Enviando a foto ao PhotoPrism"
        if album_name:
            return f"{base} (album *{album_name}*)..."
        return f"{base}..."
    if choice == "personal":
        if count > 1:
            return f"Guardando {count} arquivos no seu registro pessoal..."
        return "Guardando no seu registro pessoal..."
    return ""


def build_pending_processing_wait() -> str:
    return "Ainda estou processando seu arquivo. Aguarde um instante."


def build_pending_clarification(*, supports_gallery: bool) -> str:
    if supports_gallery:
        return (
            "Não entendi. Responda *pessoal* (arquivo seu) "
            "ou *galeria* (fotos e videos da casa)."
        )
    return (
        "Não entendi. Responda *sim* ou *guardar* para guardar no seu arquivo, "
        "ou *cancelar*."
    )


def extract_personal_label(text: str) -> str:
    """Rotulo opcional a partir da resposta do usuario."""
    m = re.search(
        r"(?:guardar|salvar|mem[oó]ria|pessoal|convite|rotulo|rótulo|registro)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()[:120]
    return ""


def _normalize_description(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _looks_like_whatsapp_filename(text: str) -> bool:
    base = text.strip().split("/")[-1]
    return bool(_WHATSAPP_FILENAME_RE.match(base))


def _is_meaningful_description(text: str) -> bool:
    norm = _normalize_description(text)
    if not norm or norm in _GENERIC_DESCRIPTIONS:
        return False
    if len(norm) < 3:
        return False
    if _looks_like_whatsapp_filename(text):
        return False
    return True


def extract_personal_description(user_text: str, caption: str = "") -> str:
    """
    Descricao util para o registro pessoal (rotulo legivel).
    Retorna vazio se nao houver certeza do conteudo.
    """
    text = user_text.strip()
    labeled = extract_personal_label(text)
    if _is_meaningful_description(labeled):
        return labeled.strip()[:120]

    remainder = _DESTINATION_PREFIX_RE.sub("", text).strip(" :,-")
    if _is_meaningful_description(remainder):
        return remainder[:120]

    if text and not any(w in text.casefold() for w in _PERSONAL_WORDS + _SAVE_WORDS):
        if _is_meaningful_description(text):
            return text[:120]

    cap = caption.strip()
    if _is_meaningful_description(cap):
        return cap[:120]

    return ""


def personal_description_required(user_text: str, caption: str = "") -> bool:
    """True se falta descricao antes de guardar no registro pessoal."""
    return not extract_personal_description(user_text, caption)


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
