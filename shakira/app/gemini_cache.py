"""Cache de contexto Gemini para catalogo estatico de dispositivos."""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

import google.generativeai as genai

from app.devices_catalog import DevicesCatalog
from app.prompts import SYSTEM_INSTRUCTION

log = logging.getLogger(__name__)

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


def _cache_exists(name: str) -> bool:
    try:
        from google.generativeai import caching

        caching.CachedContent.get(name)
        return True
    except Exception:
        return False


def ensure_catalog_cache(
    *,
    api_key: str,
    model: str,
    catalog: DevicesCatalog,
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
    full_system = f"{SYSTEM_INSTRUCTION}\n\n{catalog_text}"
    content_hash = catalog.content_hash or hash(catalog_text)

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

    try:
        cache = caching.CachedContent.create(
            model=_model_resource(model),
            display_name="shakira-catalog",
            system_instruction=full_system,
            contents=["Catalogo de dispositivos e regras de acao carregados."],
            ttl=datetime.timedelta(hours=max(1, min(ttl_hours, 168))),
        )
        _save_meta({"cache_name": cache.name, "content_hash": content_hash})
        log.info("Gemini cache atualizado: %s (hash=%s...)", cache.name, content_hash[:12])
        return cache.name
    except Exception:
        log.exception("Falha ao criar cache Gemini; usando fallback inline")
        return None
