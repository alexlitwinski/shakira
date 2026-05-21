"""Analise Gemini da imagem do interfone (visitante na porta)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import google.generativeai as genai

log = logging.getLogger(__name__)

INTERFONE_VISION_SYSTEM = """Você analisa a imagem da câmera da porta de vidro quando o interfone toca.
Responda SOMENTE em JSON válido (sem markdown).
Use português do Brasil nos campos de texto.

Formato obrigatório:
{
  "visitor_description": "descrição objetiva de quem ou o que está na porta (pessoa, entregador, vazio, etc.)",
  "visitor_type": "morador_conhecido | visitante | entregador | prestador | desconhecido | vazio | indeterminado",
  "summary": "resumo curto em uma ou duas frases para o morador (quem parece estar chamando)"
}"""


@dataclass
class InterfoneVisitorAnalysis:
    visitor_description: str = ""
    visitor_type: str = "indeterminado"
    summary: str = ""

    def whatsapp_summary(self) -> str:
        if self.summary.strip():
            return self.summary.strip()
        if self.visitor_description.strip():
            return self.visitor_description.strip()
        return "Não foi possível identificar quem está na porta."


def build_interfone_vision_prompt(*, camera_label: str = "Porta de vidro") -> str:
    return (
        f"O interfone da residência está tocando. Analise a imagem da câmera '{camera_label}'.\n"
        "Descreva quem ou o que parece estar chamando (aparência, roupa, pacote, uniforme, veículo).\n"
        "Se não houver ninguém visível, indique visitor_type=vazio."
    )


def _parse_visitor_payload(raw: str) -> InterfoneVisitorAnalysis | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return InterfoneVisitorAnalysis(
        visitor_description=str(data.get("visitor_description") or "").strip(),
        visitor_type=str(data.get("visitor_type") or "indeterminado").strip(),
        summary=str(data.get("summary") or "").strip(),
    )


def analyze_interfone_visitor(
    *,
    api_key: str,
    image_bytes: bytes,
    camera_label: str = "Porta de vidro",
    model: str | None = None,
) -> InterfoneVisitorAnalysis | None:
    key = api_key.strip()
    if not key or not image_bytes:
        return None

    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    prompt = build_interfone_vision_prompt(camera_label=camera_label)

    try:
        genai.configure(api_key=key)
        vision_model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=INTERFONE_VISION_SYSTEM,
        )
        response = vision_model.generate_content(
            [
                prompt,
                {"mime_type": "image/jpeg", "data": image_bytes},
            ],
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    except Exception:
        log.exception("Gemini analyze_interfone_visitor falhou")
        return None

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)

    analysis = _parse_visitor_payload(text)
    if analysis is None:
        log.warning("Gemini interfone: JSON invalido: %s", text[:300])
    return analysis
