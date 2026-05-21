"""Prompts Gemini para resumo da situacao da casa."""

from __future__ import annotations

import json
from typing import Any

from app.camera_vision import CameraMosaicAnalysis, format_analysis_message

HOUSE_STATUS_SYSTEM = """Você redige um resumo curto da situação atual da casa para WhatsApp.
Responda SOMENTE com texto plano em português do Brasil — sem JSON, sem markdown, sem entity_id.
Tom natural e direto, como um assistente de casa falando com o morador.
Use linguagem simples; nunca cite nomes técnicos do Home Assistant.

Estruture em 2–4 parágrafos curtos:
1. Resumo geral (tranquilo, atenção necessária, etc.)
2. O que as câmeras mostram (pessoas, movimento, áreas vazias)
3. Chuva e sensores do alarme (portas/janelas abertas, movimento, partições armadas/disparadas)
4. Se houver algo que mereça ação, diga o que fazer — senão, tranquilize.

Não repita literalmente listas longas de sensores; sintetize o que importa.
Máximo 12–15 linhas no total."""


def vision_analysis_to_facts(analysis: CameraMosaicAnalysis | None) -> dict[str, Any]:
    if analysis is None:
        return {"disponivel": False}
    cameras = [
        {
            "nome": cam.name,
            "pessoa_detectada": cam.person_detected,
            "notas": cam.notes,
        }
        for cam in analysis.cameras
    ]
    return {
        "disponivel": True,
        "cameras": cameras,
        "descricao": analysis.description,
        "recomendacao": analysis.recommendation,
        "texto_formatado": format_analysis_message(analysis),
    }


def build_house_status_prompt(
    *,
    vision_analysis: CameraMosaicAnalysis | None,
    sensor_context: str,
) -> str:
    vision_facts = json.dumps(
        vision_analysis_to_facts(vision_analysis),
        ensure_ascii=False,
        indent=2,
    )
    sensors_block = sensor_context.strip() or "(Sensores indisponíveis no momento.)"
    return f"""O morador pediu para saber como está a casa agora.

Análise das câmeras (Gemini Vision):
{vision_facts}

Estados dos sensores (chuva e alarme):
{sensors_block}

Redija uma única mensagem para WhatsApp descrevendo a situação geral da casa,
integrando o que aparece nas câmeras com os sensores de chuva e do alarme."""
