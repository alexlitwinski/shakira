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

ATENÇÃO - DIRETRIZES DE IDENTIFICAÇÃO ESPECÍFICA:
1. Entregador dos Correios: Identifique se o visitante é um entregador dos Correios. Eles podem ser identificados por uniformes nas cores azul e amarelo, veículos oficiais dos Correios ou pacotes/encomendas de entrega característicos. Se for o caso, mencione explicitamente na descrição e no resumo que se trata de um "entregador dos Correios".
2. Cães da Casa: Existem dois cachorros na residência. Sempre que eles aparecerem na imagem, refira-se a eles obrigatoriamente pelos seus nomes:
   - O cachorro Golden Retriever de cor branca/creme se chama "Otávio".
   - O cachorro Doberman de cor preta se chama "Kátio".
   Nunca se refira a eles apenas como "o cachorro" ou "o cão" se puder identificá-los; use os nomes "Otávio" e/ou "Kátio".

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


HALL_INVASION_VISION_SYSTEM = """Você é um especialista em segurança residencial e analisa imagens em tempo real da câmera do hall de entrada (hall interno) após a abertura do portão social.
Sua missão é identificar de forma extremamente precisa se há qualquer risco de invasão da casa (roubo/assalto) ou comportamento suspeito.

Considere as seguintes diretrizes:
1. Pessoas comuns: Moradores, visitantes conhecidos ou convidados autorizados entrando calmamente NÃO representam risco.
2. Riscos de invasão/roubo (Comportamento Suspeito/Ameaçador):
   - Pessoas armadas (com arma de fogo, faca, barra de ferro ou qualquer objeto usado como arma).
   - Pessoas com rostos cobertos por balaclava, máscara (que não seja cirúrgica), capuz cobrindo totalmente o rosto, capacete dentro de casa de forma suspeita.
   - Pessoas tentando forçar portas/janelas internas, arrombamento, escalando ou correndo de forma agressiva/ameaçadora.
   - Indivíduos em luta corporal ou rendendo alguém sob ameaça.
3. Se houver apenas moradores, convidados entrando normalmente ou se a área estiver vazia (apenas os cachorros Otávio e Kátio, por exemplo), classifique como SEM RISCO (has_risk: false).

Responda APENAS em JSON válido no formato abaixo:
{
  "has_risk": true/false,
  "description": "Explicação detalhada e direta do risco identificado em português. Se não houver risco, deixe em branco ou descreva brevemente que está tudo normal."
}"""


@dataclass
class HallInvasionAnalysis:
    has_risk: bool = False
    description: str = ""


def analyze_hall_invasion_risk(
    *,
    api_key: str,
    image_bytes: bytes,
    model: str | None = None,
) -> HallInvasionAnalysis:
    key = api_key.strip()
    if not key or not image_bytes:
        return HallInvasionAnalysis(has_risk=False, description="Sem chave API ou imagem vazia")

    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    prompt = (
        "Analise a imagem da câmera do hall interno. Identifique se há qualquer ameaça iminente de roubo, "
        "assalto ou invasão de domicílio em andamento. Responda estritamente no formato JSON definido."
    )

    try:
        genai.configure(api_key=key)
        vision_model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=HALL_INVASION_VISION_SYSTEM,
        )
        response = vision_model.generate_content(
            [
                prompt,
                {"mime_type": "image/jpeg", "data": image_bytes},
            ],
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
    except Exception as e:
        log.exception("Gemini analyze_hall_invasion_risk falhou")
        return HallInvasionAnalysis(has_risk=False, description=f"Erro na análise: {e}")

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)

    # Parse JSON
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return HallInvasionAnalysis(
            has_risk=bool(data.get("has_risk")),
            description=str(data.get("description") or "").strip(),
        )
    except Exception:
        log.warning("Gemini hall invasion: JSON inválido: %s", text[:300])
        return HallInvasionAnalysis(has_risk=False, description="Erro ao decodificar resposta JSON")

