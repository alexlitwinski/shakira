"""Cache de contexto Gemini para catalogo estatico de dispositivos."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from app.cameras_catalog import CamerasCatalog
from app.devices_catalog import DevicesCatalog
from app.prompts import SYSTEM_INSTRUCTION

log = logging.getLogger(__name__)

# API Gemini exige conteudo minimo para CachedContent (erro 400 se menor)
GEMINI_MIN_CACHE_TOKENS = 4096

CACHE_META_PATH = Path(os.environ.get("GEMINI_CACHE_META_PATH", "/data/shakira_gemini_cache.json"))


def _load_meta() -> dict[str, Any]:
    if not CACHE_META_PATH.is_file():
        return {}
    try:
        data = json.loads(CACHE_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_meta(data: dict[str, Any]) -> None:
    try:
        CACHE_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_META_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Nao foi possivel gravar meta do cache: %s", e)


def _model_resource(model: str) -> str:
    if model.startswith("models/"):
        return model
    return f"models/{model}"


def _delete_cache(name: str) -> None:
    try:
        from google.generativeai import caching

        caching.CachedContent.delete(name=name)
    except Exception as e:
        log.debug("Cache antigo nao removido (%s): %s", name, e)


def _estimate_cache_tokens(*, model: str, system_text: str, contents_text: str) -> int | None:
    """Conta tokens do payload do cache; None se a API nao responder."""
    payload = f"{system_text}\n\n{contents_text}"
    try:
        m = genai.GenerativeModel(model_name=model)
        result = m.count_tokens(payload)
        return int(getattr(result, "total_tokens", 0) or 0)
    except Exception as e:
        log.debug("count_tokens indisponivel: %s", e)
        return None


def _cache_too_small_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "too small" in msg or "min_total_token_count" in msg


def _cache_exists(name: str) -> bool:
    try:
        from google.generativeai import caching

        caching.CachedContent.get(name)
        return True
    except Exception:
        return False


def _combined_content_hash(devices_hash: str, cameras_hash: str) -> str:
    payload = f"{devices_hash}:{cameras_hash}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ensure_catalog_cache(
    *,
    api_key: str,
    model: str,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog | None = None,
    ttl_hours: int = 24,
) -> str | None:
    """Cria ou reutiliza cache Gemini. Retorna cache name ou None (fallback inline)."""
    try:
        from google.generativeai import caching
    except ImportError:
        log.warning("Modulo caching nao disponivel no SDK")
        return None

    genai.configure(api_key=api_key)
    catalog_text = catalog.build_catalog_context()
    cameras_text = ""
    if cameras and cameras.cameras:
        cameras_text = "\n\n" + cameras.build_catalog_context()
    full_system = f"{SYSTEM_INSTRUCTION}\n\n{catalog_text}{cameras_text}"
    content_hash = _combined_content_hash(
        catalog.content_hash or "",
        (cameras.content_hash if cameras else "") or "",
    )

    meta = _load_meta()
    existing_name = meta.get("cache_name")
    if (
        isinstance(existing_name, str)
        and existing_name
        and meta.get("content_hash") == content_hash
        and _cache_exists(existing_name)
    ):
        log.debug("Reutilizando cache Gemini: %s", existing_name)
        return existing_name

    if isinstance(existing_name, str) and existing_name:
        _delete_cache(existing_name)

    contents_text = "Catalogo de dispositivos e regras de acao carregados."
    token_count = _estimate_cache_tokens(
        model=model, system_text=full_system, contents_text=contents_text
    )
    if token_count is not None and token_count < GEMINI_MIN_CACHE_TOKENS:
        log.info(
            "Catalogo pequeno (%s tokens, minimo %s); cache Gemini omitido — fallback inline",
            token_count,
            GEMINI_MIN_CACHE_TOKENS,
        )
        return None

    try:
        cache = caching.CachedContent.create(
            model=_model_resource(model),
            display_name="shakira-catalog",
            system_instruction=full_system,
            contents=[contents_text],
            ttl=datetime.timedelta(hours=max(1, min(ttl_hours, 168))),
        )
        _save_meta({"cache_name": cache.name, "content_hash": content_hash})
        log.info("Gemini cache atualizado: %s (hash=%s...)", cache.name, content_hash[:12])
        return cache.name
    except google_exceptions.InvalidArgument as e:
        if _cache_too_small_error(e):
            log.info(
                "Cache Gemini nao criado (conteudo abaixo do minimo de %s tokens); fallback inline",
                GEMINI_MIN_CACHE_TOKENS,
            )
            return None
        log.warning("Cache Gemini rejeitado: %s", e)
        return None
    except Exception:
        log.exception("Falha ao criar cache Gemini; usando fallback inline")
        return None
