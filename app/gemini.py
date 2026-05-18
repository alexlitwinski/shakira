"""Integracao com Google Gemini para interpretar comandos e devolver JSON."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

log = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """Voce e o assistente da casa conectada ao Home Assistant.
O usuario fala em portugues. Responda sempre em portugues do Brasil.

Voce recebe:
- A mensagem do usuario
- Um resumo das entidades do Home Assistant (entity_id, estado, nome amigavel)

Sua tarefa e decidir o que fazer e responder SOMENTE com um JSON valido (sem markdown, sem ```), no formato:
{
  "action": "reply" | "call_service" | "get_state" | "list_entities",
  "domain": "light",
  "service": "turn_on",
  "service_data": { "entity_id": "light.sala" },
  "entity_id": "sensor.temperatura",
  "response": "Texto curto e claro para o usuario no WhatsApp"
}

Regras:
- Use "reply" quando for conversa, ajuda ou explicacao sem acao no HA.
- Use "call_service" para acionar servicos (ex: light.turn_on, switch.turn_off, climate.set_temperature).
  Preencha domain, service e service_data com os campos exigidos pelo HA (geralmente entity_id ou area_id, etc).
- Use "get_state" quando precisar informar o estado de UMA entidade; preencha entity_id.
- Use "list_entities" raramente; response pode explicar que muitas entidades existem.
- Se o pedido envolver varias acoes, escolha a principal ou use call_service com dados coerentes; se impossivel, reply pedindo detalhe.
- Nunca invente entity_id: use apenas ids presentes no contexto fornecido.
- Se nao tiver certeza, action=reply e peca esclarecimento.
"""


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


class GeminiAssistant:
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_INSTRUCTION,
        )

    def decide(self, *, user_message: str, entities_context: str) -> dict[str, Any]:
        prompt = f"""Contexto de entidades (resumo):
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
