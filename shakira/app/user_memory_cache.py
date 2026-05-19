"""Cache de contexto Gemini por usuario (memorias persistentes)."""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

import google.generativeai as genai

from app.gemini_cache import GEMINI_MIN_CACHE_TOKENS, _cache_exists, _cache_too_small_error, _delete_cache, _model_resource
from app.user_memory import UserMemoryStore

log = logging.getLogger(__name__)

USER_MEMORY_CACHE_INSTRUCTION = """Voce tem acesso a MEMORIA PERSISTENTE deste usuario WhatsApp.
Use essas informacoes para responder perguntas sobre o que ele pediu para guardar ou lembrar.
Nao invente fatos que nao estejam na memoria ou no contexto da conversa atual.
Para guardar nova informacao use action=save_memory.
Para reenviar um arquivo guardado use action=send_user_file com file_id ou file_name.
Para apagar anotacao ou arquivo use action=delete_from_memory — nunca send_user_file.
"""


def _load_meta(store: UserMemoryStore) -> dict[str, Any]:
    path = store.cache_meta_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_meta(store: UserMemoryStore, data: dict[str, Any]) -> None:
    path = store.cache_meta_path()
    try:
        store.ensure_dirs()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Nao foi possivel gravar meta cache usuario %s: %s", store.phone, e)


def invalidate_user_memory_cache(store: UserMemoryStore) -> None:
    meta = _load_meta(store)
    name = meta.get("cache_name")
    if isinstance(name, str) and name:
        _delete_cache(name)
    try:
        store.cache_meta_path().unlink(missing_ok=True)
    except OSError:
        pass


def ensure_user_memory_cache(
    *,
    api_key: str,
    model: str,
    store: UserMemoryStore,
    ttl_hours: int = 24,
) -> str | None:
    """Cria ou reutiliza cache Gemini com memorias do usuario. None se vazio ou pequeno."""
    context = store.build_context_text()
    if not context.strip():
        return None

    try:
        from google.generativeai import caching
    except ImportError:
        return None

    genai.configure(api_key=api_key)
    content_hash = store.content_hash()
    meta = _load_meta(store)
    existing = meta.get("cache_name")
    if (
        isinstance(existing, str)
        and existing
        and meta.get("content_hash") == content_hash
        and _cache_exists(existing)
    ):
        log.debug("Reutilizando cache memoria usuario %s: %s", store.phone, existing)
        return existing

    if isinstance(existing, str) and existing:
        _delete_cache(existing)

    full_system = f"{USER_MEMORY_CACHE_INSTRUCTION}\n\n{context}"
    contents_text = "Memorias e arquivos do usuario carregados."

    try:
        m = genai.GenerativeModel(model_name=model)
        token_result = m.count_tokens(f"{full_system}\n\n{contents_text}")
        token_count = int(getattr(token_result, "total_tokens", 0) or 0)
    except Exception:
        token_count = 0

    if token_count > 0 and token_count < GEMINI_MIN_CACHE_TOKENS:
        log.debug(
            "Memoria usuario %s pequena (%s tokens); sem cache dedicado",
            store.phone,
            token_count,
        )
        return None

    try:
        cache = caching.CachedContent.create(
            model=_model_resource(model),
            display_name=f"shakira-user-{store.phone[-8:]}",
            system_instruction=full_system,
            contents=[contents_text],
            ttl=datetime.timedelta(hours=max(1, min(ttl_hours, 168))),
        )
        _save_meta(store, {"cache_name": cache.name, "content_hash": content_hash})
        log.info("Cache memoria usuario %s: %s", store.phone, cache.name)
        return cache.name
    except Exception as e:
        if _cache_too_small_error(e):
            log.debug("Cache memoria usuario %s abaixo do minimo de tokens", store.phone)
            return None
        log.warning("Falha cache memoria usuario %s: %s", store.phone, e)
        return None
