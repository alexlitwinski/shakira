"""Integracao com Google Gemini (cache de catalogo + estados dinamicos por mensagem)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

from app.prompts import SYSTEM_INSTRUCTION

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

    def decide(self, *, user_message: str, entities_context: str) -> dict[str, Any]:
        prompt = f"""Estados atuais (resumo dinamico - todas as entidades para consulta):
{entities_context}

Mensagem do usuario:
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
            return data
        except (json.JSONDecodeError, ValueError):
            log.warning("Resposta Gemini nao-JSON: %s", raw[:300])
            return {
                "action": "reply",
                "response": raw[:2000] if raw else "Sem resposta do modelo.",
            }
