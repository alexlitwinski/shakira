"""Descricao de mosaicos de cameras via Gemini Vision."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

import google.generativeai as genai

log = logging.getLogger(__name__)

_H_POSITION = ("esquerda", "central", "direita")

DEFAULT_WATCH_CAMERA_NAMES = ("Porta de vidro", "Rua", "Rua 1")
DEFAULT_RETRY_DELAY_SECONDS = float(os.environ.get("CAMERA_VISION_RETRY_DELAY_SECONDS", "4"))
DEFAULT_MAX_VISION_RETRIES = int(os.environ.get("CAMERA_VISION_MAX_RETRIES", "1"))

CAMERA_VISION_SYSTEM = """Você analisa imagens de câmeras de segurança residenciais para o morador.
Responda SOMENTE em JSON válido (sem markdown, sem texto fora do JSON).
Use português do Brasil nos campos de texto.

OBRIGATÓRIO: use EXCLUSIVAMENTE os nomes exatos das câmeras fornecidos no mapeamento.
NUNCA use rótulos genéricos de posição como "câmera superior esquerda".

Para cada câmera, indique claramente:
- Se há pessoa visível (person_detected: true/false). Considere pessoa: adulto, criança, entregador, visitante — qualquer ser humano visível. Veículos, animais ou sombras não contam como pessoa.
- Se o cachorro Kátio (Doberman preto) está claramente visível (katio_detected: true/false).
- Se o cachorro Otávio (Golden Retriever branco/creme) está claramente visível (otavio_detected: true/false).

ATENÇÃO - DIRETRIZES DE IDENTIFICAÇÃO ESPECÍFICA:
1. Cães da Casa: Existem dois cachorros na residência. Sempre que eles aparecerem em qualquer uma das imagens das câmeras, você deve se referir a eles obrigatoriamente pelos seus nomes:
   - O cachorro Golden Retriever de cor branca/creme se chama "Otávio".
   - O cachorro Doberman de cor preta se chama "Kátio".
   Refira-se a eles pelos seus nomes ("Otávio" e/ou "Kátio") na descrição (description) e nas observações (notes). Nunca se refira a eles apenas como "o cachorro" ou "o cão" se puder identificá-los.
2. EVITE ALUCINAÇÕES E FALSOS POSITIVOS: Seja extremamente rigoroso e cético. Se um cachorro ("Otávio" ou "Kátio") ou qualquer pessoa NÃO estiver claramente visível e identificável na imagem, NUNCA afirme que está lá. É muito melhor dizer que a câmera está vazia ou sem detecção do que gerar um falso positivo. Não se deixe influenciar por expectativas de encontrar o cão ou pessoa; relate apenas o que é 100% visível de fato.
3. Entregador dos Correios: Identifique se há um entregador dos Correios (uniforme azul e amarelo, veículo dos Correios ou pacotes/encomendas). Se for o caso, mencione-o explicitamente como "entregador dos Correios" na descrição e observações.

Formato JSON obrigatório:
{
  "cameras": [
    {
      "name": "nome exato",
      "person_detected": true,
      "katio_detected": false,
      "otavio_detected": false,
      "notes": "breve descricao do que ve"
    }
  ],
  "description": "detalhes por camera (Nome: o que ve em cada uma), texto corrido para WhatsApp",
  "recommendation": "resumo curto e direto para o morador (ex.: se ha pessoa, o que fazer)"
}"""


@dataclass(frozen=True)
class CameraPanelInfo:
    name: str
    description: str = ""


@dataclass
class CameraPresence:
    name: str
    person_detected: bool = False
    katio_detected: bool = False
    otavio_detected: bool = False
    notes: str = ""


@dataclass
class CameraMosaicAnalysis:
    cameras: list[CameraPresence] = field(default_factory=list)
    description: str = ""
    recommendation: str = ""


@dataclass(frozen=True)
class HouseStatusMosaicInput:
    area_label: str
    image_bytes: bytes
    camera_panels: tuple[CameraPanelInfo, ...] = ()


HOUSE_STATUS_VISION_SYSTEM = """Você analisa mosaicos de câmeras de segurança residenciais para o morador.
Receberá até 3 imagens JPEG separadas — cada uma precedida pelo rótulo da área (Interna, Portão Social ou Externas).
Analise TODAS as imagens fornecidas; não omita nenhuma área.

Responda SOMENTE em JSON válido (sem markdown, sem texto fora do JSON).
Use português do Brasil nos campos de texto.

OBRIGATÓRIO: use EXCLUSIVAMENTE os nomes exatos das câmeras fornecidos no mapeamento de cada área.
NUNCA use rótulos genéricos de posição como "câmera superior esquerda".

Para cada câmera, indique claramente se há pessoa visível (person_detected: true/false).
Considere pessoa: adulto, criança, entregador, visitante — qualquer ser humano visível.

ATENÇÃO - DIRETRIZES DE IDENTIFICAÇÃO ESPECÍFICA:
1. Cães da Casa: Existem dois cachorros na residência. Sempre que eles aparecerem em qualquer uma das imagens, você deve se referir a eles obrigatoriamente pelos seus nomes:
   - O cachorro Golden Retriever de cor branca/creme se chama "Otávio".
   - O cachorro Doberman de cor preta se chama "Kátio".
   Refira-se a eles pelos seus nomes ("Otávio" e/ou "Kátio") na descrição (description) e nas observações (notes). Nunca se refira a eles apenas como "o cachorro" ou "o cão" se puder identificá-los.
2. EVITE ALUCINAÇÕES E FALSOS POSITIVOS: Seja extremamente rigoroso e cético. Se um cachorro ("Otávio" ou "Kátio") ou qualquer pessoa NÃO estiver claramente visível e identificável na imagem, NUNCA afirme que está lá. É muito melhor dizer que a câmera está vazia ou sem detecção do que gerar um falso positivo. Relate apenas o que é 100% visível de fato.
3. Entregador dos Correios: Identifique se há um entregador dos Correios (uniforme azul e amarelo, veículo dos Correios ou pacotes/encomendas). Se for o caso, mencione-o explicitamente como "entregador dos Correios" na descrição e observações.

Formato JSON obrigatório:
{
  "areas": [
    {
      "area": "nome exato da área (Interna, Portão Social ou Externas)",
      "cameras": [
        {"name": "nome exato", "person_detected": true, "notes": "breve descricao do que ve"}
      ],
      "description": "detalhes por câmera desta área (Nome: o que vê)",
      "recommendation": "resumo curto desta área"
    }
  ]
}"""


def grid_position_label(index: int, total: int) -> str:
    """Rotulo de posicao no grid (mesma logica de image_collage.build_image_grid)."""
    if total <= 1:
        return "unico painel"
    cols = min(3, total) if total <= 9 else 3
    rows = math.ceil(total / cols)
    row, col = divmod(index, cols)
    if rows == 1:
        return _H_POSITION[col] if col < len(_H_POSITION) else f"coluna {col + 1}"
    vertical = "superior" if row == 0 else "inferior"
    horizontal = _H_POSITION[col] if col < len(_H_POSITION) else f"coluna {col + 1}"
    return f"{vertical} {horizontal}"


def _normalize_camera_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def camera_names_match(panel_name: str, watch_key: str) -> bool:
    """True se o nome do painel corresponde a uma camera monitorada."""
    panel = _normalize_camera_name(panel_name)
    watch = _normalize_camera_name(watch_key)
    if panel == watch:
        return True
    if watch == "rua 1" and panel == "rua":
        return True
    if watch == "rua" and panel == "rua":
        return True
    return False


def watched_panel_names(
    panels: Sequence[CameraPanelInfo],
    watch_names: Sequence[str] | None = None,
) -> list[str]:
    keys = watch_names or DEFAULT_WATCH_CAMERA_NAMES
    matched: list[str] = []
    for panel in panels:
        if any(camera_names_match(panel.name, key) for key in keys):
            matched.append(panel.name)
    return matched


def should_retry_for_missing_person(
    analysis: CameraMosaicAnalysis,
    panels: Sequence[CameraPanelInfo],
    watch_names: Sequence[str] | None = None,
) -> bool:
    """
    True se nenhuma das cameras monitoradas (ex.: Porta de vidro, Rua) tem pessoa.
    """
    targets = watched_panel_names(panels, watch_names)
    if not targets:
        return False

    for target in targets:
        for cam in analysis.cameras:
            if camera_names_match(cam.name, target) and cam.person_detected:
                return False
    return True


def build_retry_notice(watch_names: Sequence[str] | None = None) -> str:
    keys = list(watch_names or DEFAULT_WATCH_CAMERA_NAMES)
    if len(keys) >= 2:
        names = f"{keys[0]} nem na {keys[1]}"
    elif keys:
        names = keys[0]
    else:
        names = "Porta de vidro nem na Rua"
    return (
        f"Não identifiquei ninguém na {names}. "
        "Aguardando alguns segundos e capturando novas imagens..."
    )


def build_camera_mosaic_prompt(
    *,
    camera_panels: Sequence[CameraPanelInfo],
    context: str = "",
) -> str:
    panels = [p for p in camera_panels if p.name.strip()]
    mapping_block = ""
    if panels:
        lines: list[str] = []
        for index, panel in enumerate(panels):
            position = grid_position_label(index, len(panels))
            line = f"{index + 1}. Painel {position} → **{panel.name.strip()}**"
            if panel.description.strip():
                line += f" ({panel.description.strip()})"
            lines.append(line)
        mapping_block = (
            "\nMAPEAMENTO DO MOSAICO (posição no grid → nome da câmera):\n"
            + "\n".join(lines)
            + "\n\n"
            "Use estes nomes exatos no JSON (campo name) e na description.\n"
            "Ao descrever, inicie cada trecho pelo NOME da câmera "
            '(ex.: "Na Porta de vidro...", "Na Rua...").\n'
        )

    context_block = ""
    if context.strip():
        context_block = f"\nContexto do alerta: {context.strip()}\n"

    return f"""Analise a imagem em mosaico das câmeras de segurança.
{context_block}{mapping_block}
Descreva o que aparece em cada câmera. Para cada uma, defina person_detected com precisão.
Se alguma câmera estiver escura, vazia ou sem movimento relevante, diga isso em notes.
Monte recommendation como resumo geral curto e description com detalhe de cada câmera (Nome: ...)."""


def _parse_analysis_payload(raw: str) -> CameraMosaicAnalysis | None:
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

    cameras: list[CameraPresence] = []
    for row in data.get("cameras") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        cameras.append(
            CameraPresence(
                name=name,
                person_detected=bool(row.get("person_detected")),
                katio_detected=bool(row.get("katio_detected")),
                otavio_detected=bool(row.get("otavio_detected")),
                notes=str(row.get("notes") or "").strip(),
            )
        )

    description = str(data.get("description") or "").strip()
    recommendation = str(data.get("recommendation") or "").strip()
    if not description and not recommendation:
        return None

    return CameraMosaicAnalysis(
        cameras=cameras,
        description=description,
        recommendation=recommendation,
    )


def _parse_house_status_mosaics_payload(
    raw: str,
    expected_areas: Sequence[str],
) -> list[tuple[str, CameraMosaicAnalysis]] | None:
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

    sections: list[tuple[str, CameraMosaicAnalysis]] = []
    for row in data.get("areas") or []:
        if not isinstance(row, dict):
            continue
        area = str(row.get("area") or "").strip()
        if not area:
            continue
        cameras: list[CameraPresence] = []
        for cam_row in row.get("cameras") or []:
            if not isinstance(cam_row, dict):
                continue
            name = str(cam_row.get("name") or "").strip()
            if not name:
                continue
            cameras.append(
                CameraPresence(
                    name=name,
                    person_detected=bool(cam_row.get("person_detected")),
                    notes=str(cam_row.get("notes") or "").strip(),
                )
            )
        description = str(row.get("description") or "").strip()
        recommendation = str(row.get("recommendation") or "").strip()
        if not description and not recommendation and not cameras:
            continue
        sections.append(
            (
                area,
                CameraMosaicAnalysis(
                    cameras=cameras,
                    description=description,
                    recommendation=recommendation,
                ),
            )
        )

    if not sections:
        return None

    if expected_areas:
        found = {_normalize_camera_name(a) for a, _ in sections}
        for expected in expected_areas:
            if _normalize_camera_name(expected) not in found:
                log.warning(
                    "analyze_house_status_mosaics: area ausente no JSON: %s",
                    expected,
                )
    return sections


def build_house_status_mosaics_prompt(
    mosaics: Sequence[HouseStatusMosaicInput],
    *,
    context: str = "",
) -> str:
    lines: list[str] = [
        "Analise cada mosaico abaixo. Cada imagem JPEG corresponde a uma área da casa.",
        "Retorne uma entrada em 'areas' para CADA área listada.",
    ]
    if context.strip():
        lines.append(f"\nContexto: {context.strip()}")
    for mosaic in mosaics:
        panels = [p for p in mosaic.camera_panels if p.name.strip()]
        lines.append(f"\nÁrea: {mosaic.area_label}")
        if panels:
            lines.append("Mapeamento deste mosaico (posição no grid → nome da câmera):")
            for index, panel in enumerate(panels):
                position = grid_position_label(index, len(panels))
                line = f"  {index + 1}. Painel {position} → {panel.name.strip()}"
                if panel.description.strip():
                    line += f" ({panel.description.strip()})"
                lines.append(line)
        lines.append(f"(imagem JPEG da área {mosaic.area_label} segue abaixo)")
    return "\n".join(lines)


def analyze_house_status_mosaics(
    *,
    api_key: str,
    mosaics: Sequence[HouseStatusMosaicInput],
    context: str = "",
    model: str | None = None,
) -> list[tuple[str, CameraMosaicAnalysis]]:
    """Analisa os mosaicos Interna, Portão Social e Externas numa única chamada Gemini Vision."""
    key = api_key.strip()
    valid = [m for m in mosaics if m.image_bytes and m.area_label.strip()]
    if not key or not valid:
        if not key:
            log.warning("analyze_house_status_mosaics: gemini_api_key ausente")
        return []

    if len(valid) == 1:
        single = analyze_camera_mosaic(
            api_key=key,
            image_bytes=valid[0].image_bytes,
            camera_panels=list(valid[0].camera_panels),
            context=f"{context} — área {valid[0].area_label}.".strip(" —"),
            model=model,
        )
        return [(valid[0].area_label, single)] if single else []

    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    prompt = build_house_status_mosaics_prompt(valid, context=context)
    content: list[Any] = [prompt]
    for mosaic in valid:
        content.append(f"\nImagem da área: {mosaic.area_label}\n")
        content.append({"mime_type": "image/jpeg", "data": mosaic.image_bytes})

    expected_areas = [m.area_label for m in valid]
    try:
        genai.configure(api_key=key)
        vision_model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=HOUSE_STATUS_VISION_SYSTEM,
        )
        response = vision_model.generate_content(
            content,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    except Exception:
        log.exception("Gemini analyze_house_status_mosaics falhou; fallback por area")
        return _analyze_house_status_mosaics_fallback(key, valid, context=context, model=model_name)

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)

    sections = _parse_house_status_mosaics_payload(text, expected_areas)
    if sections and len(sections) >= len(valid):
        log.info(
            "analyze_house_status_mosaics OK areas=%s",
            [a for a, _ in sections],
        )
        return sections

    log.warning(
        "analyze_house_status_mosaics: JSON incompleto (%s areas); fallback por area",
        len(sections or []),
    )
    return _analyze_house_status_mosaics_fallback(key, valid, context=context, model=model_name)


def _analyze_house_status_mosaics_fallback(
    api_key: str,
    mosaics: Sequence[HouseStatusMosaicInput],
    *,
    context: str = "",
    model: str | None = None,
) -> list[tuple[str, CameraMosaicAnalysis]]:
    sections: list[tuple[str, CameraMosaicAnalysis]] = []
    for mosaic in mosaics:
        analysis = analyze_camera_mosaic(
            api_key=api_key,
            image_bytes=mosaic.image_bytes,
            camera_panels=list(mosaic.camera_panels),
            context=f"{context} — área {mosaic.area_label}.".strip(" —"),
            model=model,
        )
        if analysis:
            sections.append((mosaic.area_label, analysis))
    return sections


def format_analysis_message(analysis: CameraMosaicAnalysis) -> str:
    """WhatsApp: resumo (recommendation) primeiro, detalhes por câmera (description) depois."""
    parts: list[str] = []
    if analysis.recommendation.strip():
        parts.append(analysis.recommendation.strip())
    if analysis.description.strip():
        if parts:
            parts.append("")
        parts.append(analysis.description.strip())
    return "\n".join(parts).strip()[:4000]


def analyze_camera_mosaic(
    *,
    api_key: str,
    image_bytes: bytes,
    camera_panels: Sequence[CameraPanelInfo] | None = None,
    context: str = "",
    model: str | None = None,
) -> CameraMosaicAnalysis | None:
    """Analisa mosaico JPEG via Gemini Vision (JSON estruturado)."""
    key = api_key.strip()
    if not key or not image_bytes:
        if not key:
            log.warning("analyze_camera_mosaic: gemini_api_key ausente")
        return None

    panels = list(camera_panels or [])
    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    prompt = build_camera_mosaic_prompt(camera_panels=panels, context=context)

    try:
        genai.configure(api_key=key)
        vision_model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=CAMERA_VISION_SYSTEM,
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
        log.exception("Gemini analyze_camera_mosaic falhou")
        return None

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)

    analysis = _parse_analysis_payload(text)
    if analysis is None:
        log.warning("Gemini analyze_camera_mosaic: JSON invalido: %s", text[:300])
    return analysis


def describe_camera_mosaic(
    *,
    api_key: str,
    image_bytes: bytes,
    camera_panels: Sequence[CameraPanelInfo] | None = None,
    camera_labels: Sequence[str] | None = None,
    context: str = "",
    model: str | None = None,
) -> str:
    """Compat: retorna apenas o texto formatado da analise."""
    panels = list(camera_panels or [])
    if not panels and camera_labels:
        panels = [CameraPanelInfo(name=label) for label in camera_labels if label.strip()]
    analysis = analyze_camera_mosaic(
        api_key=api_key,
        image_bytes=image_bytes,
        camera_panels=panels,
        context=context,
        model=model,
    )
    if analysis is None:
        return ""
    return format_analysis_message(analysis)
