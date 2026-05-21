"""Prompts Gemini para resumo da situacao da casa."""

from __future__ import annotations

import json
from typing import Any

from app.camera_vision import CameraMosaicAnalysis, format_analysis_message

HOUSE_STATUS_SYSTEM = """Você redige um resumo curto da situação atual da casa para WhatsApp.
Responda SOMENTE com texto plano em português do Brasil — sem JSON, sem markdown, sem entity_id.
Tom natural e direto, como um assistente de casa falando com o morador.
Use linguagem simples; nunca cite nomes técnicos do Home Assistant.

Estruture em 2–5 parágrafos curtos:
1. Resumo geral (tranquilo, atenção necessária, etc.)
2. O que as câmeras mostram, por área (Interna, Portão Social, Externas) — pessoas, movimento, áreas vazias
3. Chuva e sensores do alarme (portas/janelas abertas, movimento, partições armadas/disparadas)
4. Se houver dispositivos com problema (indisponíveis, offline, falha de conexão), liste-os
   brevemente num parágrafo separado — omita esta secção se não houver nenhum problema.
5. Se houver algo que mereça ação, diga o que fazer — senão, tranquilize.

Não repita literalmente listas longas de sensores; sintetize o que importa.
Máximo 12–18 linhas no total."""


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


def vision_sections_to_facts(
    sections: list[tuple[str, CameraMosaicAnalysis]],
) -> dict[str, Any]:
    if not sections:
        return {"disponivel": False}
    areas = []
    for label, analysis in sections:
        facts = vision_analysis_to_facts(analysis)
        areas.append(
            {
                "area": label,
                "cameras": facts.get("cameras", []),
                "descricao": facts.get("descricao", ""),
                "recomendacao": facts.get("recomendacao", ""),
            }
        )
    return {"disponivel": True, "areas": areas}


def build_house_status_prompt(
    *,
    vision_sections: list[tuple[str, CameraMosaicAnalysis]] | None = None,
    vision_analysis: CameraMosaicAnalysis | None = None,
    sensor_context: str,
    problems_context: str = "",
) -> str:
    sections = vision_sections or []
    if not sections and vision_analysis:
        sections = [("Câmeras", vision_analysis)]
    vision_facts = json.dumps(
        vision_sections_to_facts(sections),
        ensure_ascii=False,
        indent=2,
    )
    sensors_block = sensor_context.strip() or "(Sensores indisponíveis no momento.)"
    problems_block = problems_context.strip() or "(Nenhum dispositivo com problema detectado.)"
    return f"""O morador pediu para saber como está a casa agora.

Análise das câmeras por área (Gemini Vision — Interna, Portão Social, Externas):
{vision_facts}

Estados dos sensores (chuva e alarme):
{sensors_block}

Dispositivos com problema (indisponíveis, offline, etc.):
{problems_block}

Redija uma única mensagem para WhatsApp descrevendo a situação geral da casa,
integrando o que aparece nas câmeras com os sensores de chuva e do alarme.
Se houver dispositivos com problema, mencione-os num parágrafo separado; se a lista
indicar que não há problemas, omita essa parte."""
