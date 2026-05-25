"""Integracao com Google Gemini (cache de catalogo + estados dinamicos por mensagem)."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import google.generativeai as genai

from app.gemini_parse import parse_gemini_response_text, wrap_decisions_for_handler
from app.prompts import SYSTEM_INSTRUCTION
from app.scheduled_response_prompts import SCHEDULED_REPLY_SYSTEM, build_scheduled_reply_prompt

log = logging.getLogger(__name__)

_model_instance_cache: dict[tuple[str, str], genai.GenerativeModel] = {}


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def invalidate_any_matching_cache(cache_name: str) -> None:
    """Procura e remove qualquer arquivo de metadados de cache (catalog ou usuario) associado ao cache_name."""
    if not cache_name:
        return
    log.warning("Invalidando cache expirado ou corrompido: %s", cache_name)

    # 1. Tentar invalidar o cache de catálogo estático
    try:
        from app.gemini_cache import CACHE_META_PATH, _delete_cache
        if CACHE_META_PATH.is_file():
            try:
                data = json.loads(CACHE_META_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict) and data.get("cache_name") == cache_name:
                _delete_cache(cache_name)
                CACHE_META_PATH.unlink(missing_ok=True)
                log.info("Cache de catálogo '%s' removido do disco com sucesso.", cache_name)
    except Exception as e:
        log.debug("Erro ao invalidar cache de catálogo: %s", e)

    # 2. Tentar invalidar cache de memória de qualquer usuário
    try:
        from app.user_memory import USER_DATA_ROOT
        from app.user_memory_cache import _delete_cache
        if USER_DATA_ROOT.is_dir():
            for p in USER_DATA_ROOT.glob("*/gemini_memory_cache.json"):
                try:
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        data = {}
                    if isinstance(data, dict) and data.get("cache_name") == cache_name:
                        _delete_cache(cache_name)
                        p.unlink(missing_ok=True)
                        log.info("Cache de memória de usuário '%s' em %s removido com sucesso.", cache_name, p)
                except Exception as p_err:
                    log.debug("Erro ao verificar arquivo de cache de usuário %s: %s", p, p_err)
    except Exception as e:
        log.debug("Erro ao invalidar caches de usuários: %s", e)


class GeminiAssistant:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        *,
        cache_name: str | None = None,
        catalog_fallback: str = "",
    ) -> None:
        genai.configure(api_key=api_key)
        self._model_name = model
        self._cache_name = cache_name
        self._catalog_fallback = catalog_fallback
        self._model = self._build_model()

    def _build_model(self) -> genai.GenerativeModel:
        if self._cache_name:
            cache_key = (self._cache_name, self._model_name)
            cached = _model_instance_cache.get(cache_key)
            if cached is not None:
                return cached
            try:
                from google.generativeai import caching

                cache = caching.CachedContent.get(self._cache_name)
                log.debug("Modelo com cache Gemini: %s", self._cache_name)
                model = genai.GenerativeModel.from_cached_content(cached_content=cache)
                _model_instance_cache[cache_key] = model
                return model
            except Exception:
                log.warning("Cache Gemini indisponivel; fallback inline")

        fallback_key = (f"inline:{hash(self._catalog_fallback)}", self._model_name)
        cached_fb = _model_instance_cache.get(fallback_key)
        if cached_fb is not None:
            return cached_fb

        system = SYSTEM_INSTRUCTION
        if self._catalog_fallback:
            system = f"{system}\n\n{self._catalog_fallback}"
        model = genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
        )
        _model_instance_cache[fallback_key] = model
        return model

    def decide(
        self,
        *,
        user_message: str,
        entities_context: str,
        conversation_history: str = "",
        user_memory_context: str = "",
        memory_in_cache: bool = False,
    ) -> dict[str, Any]:
        history_block = ""
        if conversation_history.strip():
            history_block = f"{conversation_history.strip()}\n\n"

        memory_block = ""
        if user_memory_context.strip():
            if memory_in_cache:
                memory_block = (
                    "[Memória persistente do usuário carregada no cache Gemini — "
                    "use o system instruction do cache.]\n\n"
                )
            else:
                memory_block = f"{user_memory_context.strip()}\n\n"

        prompt = f"""{history_block}{memory_block}Estados atuais (entidades do catalogo shakira_devices):
{entities_context}

Mensagem atual do usuario:
{user_message}
"""
        log.debug(
            "Gemini decide prompt_chars=%s history=%s entities=%s user=%s cache=%s",
            len(prompt),
            len(conversation_history),
            len(entities_context),
            len(user_message),
            self._cache_name or "inline",
        )
        t0 = time.monotonic()
        try:
            response = self._model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
        except Exception as e:
            err_str = str(e).lower()
            is_cache_err = self._cache_name and (
                "cachedcontent not found" in err_str
                or "permission_denied" in err_str
                or "permission denied" in err_str
                or "403" in err_str
                or "404" in err_str
            )
            if is_cache_err:
                log.warning("Cache '%s' falhou (%s). Reconstruindo modelo inline e tentando novamente...", self._cache_name, e)
                # Invalida dos caches em runtime da própria instância
                cache_key = (self._cache_name, self._model_name)
                _model_instance_cache.pop(cache_key, None)
                
                # Invalida os arquivos de metadados persistentes de cache
                invalidate_any_matching_cache(self._cache_name)
                
                # Reconstrói modelo sem o cache
                self._cache_name = None
                self._model = self._build_model()
                
                # Tenta geração novamente com o modelo inline fallback
                try:
                    response = self._model.generate_content(
                        prompt,
                        generation_config=genai.GenerationConfig(
                            response_mime_type="application/json",
                            temperature=0.2,
                        ),
                    )
                except Exception as retry_err:
                    log.exception("Gemini generate_content falhou mesmo no fallback inline")
                    return {
                        "action": "reply",
                        "response": "Não consegui processar agora. Tente de novo em instantes.",
                    }
            else:
                log.exception("Gemini generate_content falhou")
                return {
                    "action": "reply",
                    "response": "Não consegui processar agora. Tente de novo em instantes.",
                }
        log.debug("Gemini decide OK (%.0fms)", (time.monotonic() - t0) * 1000.0)

        text = getattr(response, "text", None) or ""
        if not text and response.candidates:
            parts = response.candidates[0].content.parts
            text = "".join(getattr(p, "text", "") for p in parts)

        decisions = parse_gemini_response_text(text)
        wrapped = wrap_decisions_for_handler(decisions)
        action = str(wrapped.get("action") or "reply")
        if action == "_batch":
            batch = wrapped.get("batch") or []
            actions = [
                str(d.get("action") or "?") for d in batch if isinstance(d, dict)
            ]
            log.info("Gemini JSON batch count=%s actions=%s", len(batch), actions[:12])
        else:
            log.info(
                "Gemini JSON action=%s entity_id=%s domain=%s service=%s",
                action,
                wrapped.get("entity_id"),
                wrapped.get("domain"),
                wrapped.get("service"),
            )
        return wrapped

    def generate_scheduled_reply(
        self,
        *,
        context: str,
        trigger_summary: str,
        entity_states_block: str = "",
        conversation_history: str = "",
    ) -> str:
        """Gera texto proactivo para disparo de agendamento (reply-only, sem JSON)."""
        prompt = build_scheduled_reply_prompt(
            context=context,
            trigger_summary=trigger_summary,
            entity_states_block=entity_states_block,
            conversation_history=conversation_history,
        )
        try:
            model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=SCHEDULED_REPLY_SYSTEM,
            )
            response = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    temperature=0.3,
                ),
            )
        except Exception:
            log.exception("Gemini generate_scheduled_reply falhou")
            return ""

        text = getattr(response, "text", None) or ""
        if not text and response.candidates:
            parts = response.candidates[0].content.parts
            text = "".join(getattr(p, "text", "") for p in parts)
        return text.strip()[:2000]
