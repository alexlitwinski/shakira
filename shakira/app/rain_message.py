"""Mensagens WhatsApp da rotina de chuva (Gemini + fallback)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import google.generativeai as genai

log = logging.getLogger(__name__)

DEFAULT_PORTA_VIDRO_LABEL = "Porta de vidro da cozinha gourmet"
DEFAULT_TOLDO_LABEL = "Toldo da área gourmet"

RAIN_WHATSAPP_SYSTEM = """Você redige avisos curtos de WhatsApp para o morador da casa.
Responda SOMENTE com texto plano em português do Brasil — sem JSON, sem markdown.
Tom direto, natural e amigável, como um assistente de casa.
NÃO copie legendas técnicas do Home Assistant (ex.: open=, closed=, parênteses longos).
Use apenas os nomes curtos e os estados em linguagem humana (aberta, fechada, recolhido, estendido).
Se houver janelas abertas, avise e peça para fechar.
Se o toldo estiver fechado/estendido com chuva, peça para abrir/recolher o toldo.
Se a porta de vidro estiver aberta, sugira fechar.
Se tudo estiver em ordem, tranquilize o morador.
Máximo 6–8 linhas curtas."""

_TECH_SUFFIX_RE = re.compile(r"\s*\([^)]*(?:open|closed|on|off)\s*=", re.I)


@dataclass(frozen=True)
class RainStartStatus:
    open_windows: list[str]
    porta_vidro_open: bool
    toldo_closed: bool
    porta_label: str = DEFAULT_PORTA_VIDRO_LABEL
    toldo_label: str = DEFAULT_TOLDO_LABEL


def short_entity_label(description: str, fallback: str) -> str:
    """Nome amigável sem sufixos técnicos do YAML."""
    text = (description or "").strip() or fallback
    text = _TECH_SUFFIX_RE.split(text)[0].strip()
    if "—" in text:
        text = text.split("—", 1)[0].strip()
    return text or fallback


def build_rain_started_message(status: RainStartStatus) -> str:
    """Fallback se Gemini indisponivel."""
    lines = ["Começou a chover.", ""]

    if status.open_windows:
        lines.append(f"Janelas abertas: {', '.join(status.open_windows)}. Feche-as.")
    else:
        lines.append("Nenhuma janela aberta.")

    if status.toldo_closed:
        lines.append(
            f"O {status.toldo_label} está estendido (fechado) — abra/recolha o toldo."
        )
    else:
        lines.append(f"O {status.toldo_label} está recolhido.")

    if status.porta_vidro_open:
        lines.append(f"A {status.porta_label} está aberta — considere fechar.")
    else:
        lines.append(f"A {status.porta_label} está fechada.")

    return "\n".join(lines)


def rain_started_facts(status: RainStartStatus) -> dict[str, Any]:
    return {
        "evento": "comecou_a_chover",
        "janelas_abertas": list(status.open_windows),
        "toldo": {
            "nome": status.toldo_label,
            "estado": "fechado_estendido" if status.toldo_closed else "aberto_recolhido",
            "precisa_acao": status.toldo_closed,
            "acao_sugerida": "abrir/recolher o toldo" if status.toldo_closed else None,
        },
        "porta_vidro_gourmet": {
            "nome": status.porta_label,
            "estado": "aberta" if status.porta_vidro_open else "fechada",
            "precisa_acao": status.porta_vidro_open,
            "acao_sugerida": "fechar a porta" if status.porta_vidro_open else None,
        },
    }


def build_rain_started_prompt(status: RainStartStatus) -> str:
    facts = json.dumps(rain_started_facts(status), ensure_ascii=False, indent=2)
    return f"""O sensor de chuva indicou que começou a chover agora.

Dados objetivos (use para redigir a mensagem):
{facts}

Escreva uma única mensagem para WhatsApp informando o morador."""


def generate_rain_started_whatsapp(api_key: str, status: RainStartStatus) -> str:
    """Gera texto natural via Gemini; string vazia se falhar."""
    key = (api_key or "").strip()
    if not key:
        return ""
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=RAIN_WHATSAPP_SYSTEM,
        )
        response = model.generate_content(
            build_rain_started_prompt(status),
            generation_config=genai.GenerationConfig(temperature=0.35),
        )
    except Exception:
        log.exception("Gemini formatacao mensagem de chuva falhou")
        return ""

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)
    return (text or "").strip()[:4000]
