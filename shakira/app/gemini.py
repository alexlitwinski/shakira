"""Integracao com Google Gemini (cache de catalogo + estados dinamicos por mensagem)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

from app.prompts import SYSTEM_INSTRUCTION
from app.scheduled_response_prompts import SCHEDULED_REPLY_SYSTEM, build_scheduled_reply_prompt

log = logging.getLogger(__name__)


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


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
            try:
                from google.generativeai import caching

                cache = caching.CachedContent.get(self._cache_name)
                log.debug("Modelo com cache Gemini: %s", self._cache_name)
                return genai.GenerativeModel.from_cached_content(cached_content=cache)
            except Exception:
                log.warning("Cache Gemini indisponivel; fallback inline")

        system = SYSTEM_INSTRUCTION
        if self._catalog_fallback:
            system = f"{system}\n\n{self._catalog_fallback}"
        return genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
        )

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
                    "[Memoria persistente do usuario carregada no cache Gemini — "
                    "use o system instruction do cache.]\n\n"
                )
            else:
                memory_block = f"{user_memory_context.strip()}\n\n"

        prompt = f"""{history_block}{memory_block}Estados atuais (entidades do catalogo shakira_devices):
{entities_context}

Mensagem atual do usuario:
{user_message}
"""
        try:
            response = self._model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
        except Exception:
            log.exception("Gemini generate_content falhou")
            return {
                "action": "reply",
                "response": "Nao consegui processar agora. Tente de novo em instantes.",
            }

        text = getattr(response, "text", None) or ""
        if not text and response.candidates:
            parts = response.candidates[0].content.parts
            text = "".join(getattr(p, "text", "") for p in parts)

        raw = _strip_code_fence(text)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            action = str(data.get("action") or "reply")
            log.info("Gemini JSON action=%s entity_id=%s domain=%s service=%s", action, data.get("entity_id"), data.get("domain"), data.get("service"))
            return data
        except (json.JSONDecodeError, ValueError):
            log.warning("Resposta Gemini nao-JSON: %s", raw[:300])
            return {
                "action": "reply",
                "response": raw[:2000] if raw else "Sem resposta do modelo.",
            }

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
