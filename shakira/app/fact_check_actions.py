"""Rotina de verificacao de noticias via Google Fact Check Tools."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import AppSettings
from app.fact_check_client import search_fact_checked_claims_with_fallback
from app.whatsapp_steps import StepMessenger, pulse_whatsapp_typing

log = logging.getLogger(__name__)

_MAX_REVIEWS = 5
_MAX_CLAIMS = 3

_RATING_PT = {
    "false": "Falso",
    "mostly false": "Majoritariamente falso",
    "half true": "Meio verdadeiro",
    "mostly true": "Majoritariamente verdadeiro",
    "true": "Verdadeiro",
    "pants on fire": "Completamente falso",
    "incorrect": "Incorreto",
    "correct": "Correto",
    "misleading": "Enganoso",
    "unproven": "Não comprovado",
    "verdadeiro": "Verdadeiro",
    "falso": "Falso",
    "enganoso": "Enganoso",
    "exagerado": "Exagerado",
    "distortion": "Distorcao",
}


def fact_check_api_key(settings: AppSettings) -> str:
    key = (settings.google_fact_check_api_key or "").strip()
    if key:
        return key
    return (settings.gemini_api_key or "").strip()


def fact_check_configured(settings: AppSettings) -> bool:
    if not settings.fact_check_enabled:
        return False
    return bool(fact_check_api_key(settings))


def format_fact_check_api_footer(*, when: datetime | None = None) -> str:
    """Rodape fixo para confirmar no WhatsApp que a API foi consultada."""
    tz = ZoneInfo("America/Sao_Paulo")
    dt = (when or datetime.now(timezone.utc)).astimezone(tz)
    stamp = dt.strftime("%H:%M")
    return f"✓ Consulta à API Google Fact Check concluída às {stamp} (horário de Brasília)."


def extract_fact_check_query(decision: dict[str, Any]) -> str:
    return str(decision.get("fact_check_query") or "").strip()


def extract_fact_check_language(decision: dict[str, Any]) -> str:
    lang = str(decision.get("fact_check_language") or "").strip()
    return lang or "pt-BR"


def _format_review_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return raw[:10]


def _translate_rating(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "Sem classificação"
    mapped = _RATING_PT.get(text.casefold())
    return mapped or text


def format_fact_check_response(
    query: str,
    claims: list[dict[str, Any]],
    *,
    language_note: str = "",
) -> str:
    q = (query or "").strip()
    if not claims:
        lines = [
            f'Não encontrei verificações de fact-check publicadas sobre:\n"{q}"',
            "",
            "Isso não prova que a informação seja verdadeira ou falsa — apenas que "
            "nenhum verificador indexado pelo Google Fact Check Tools analisou esse tema "
            "com essas palavras.",
            "",
            "Sugestão: reformule com termos mais específicos ou envie o trecho exato da notícia.",
        ]
        return "\n".join(lines)

    lines = [f'Fact-check sobre:\n"{q}"']
    if language_note:
        lines.append(f"(Busca: {language_note})")
    lines.append("")

    shown = 0
    for claim in claims[:_MAX_CLAIMS]:
        claim_text = str(claim.get("text") or "").strip()
        claimant = str(claim.get("claimant") or "").strip()
        reviews = claim.get("claimReview") or []
        if not isinstance(reviews, list):
            continue

        for review in reviews:
            if not isinstance(review, dict):
                continue
            rating = _translate_rating(str(review.get("textualRating") or ""))
            publisher = review.get("publisher") or {}
            pub_name = ""
            if isinstance(publisher, dict):
                pub_name = str(publisher.get("name") or publisher.get("site") or "").strip()
            url = str(review.get("url") or "").strip()
            title = str(review.get("title") or "").strip()
            review_date = _format_review_date(str(review.get("reviewDate") or ""))

            shown += 1
            block = [f"{shown}."]
            if claim_text:
                block.append(f'Alegação: "{claim_text}"')
            if claimant:
                block.append(f"Quem disse: {claimant}")
            block.append(f"Veredito: *{rating}*")
            if pub_name:
                block.append(f"Verificador: {pub_name}")
            if review_date:
                block.append(f"Data: {review_date}")
            if title:
                block.append(f"Título: {title}")
            if url:
                block.append(f"Fonte: {url}")
            lines.append("\n".join(block))
            lines.append("")

            if shown >= _MAX_REVIEWS:
                break
        if shown >= _MAX_REVIEWS:
            break

    lines.append(
        "Baseado em verificadores indexados pelo Google Fact Check Tools. "
        "Leia a fonte completa antes de concluir."
    )
    return "\n".join(lines).strip()


async def handle_fact_check_claim(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    http: httpx.AsyncClient,
    messenger: StepMessenger | None = None,
) -> str:
    query = extract_fact_check_query(decision)
    if not query:
        return (
            str(decision.get("response") or "").strip()
            or "Preciso saber qual notícia ou alegação devo verificar."
        )

    if not fact_check_configured(settings):
        return (
            "A verificação de notícias ainda não está configurada. "
            "Ative a Google Fact Check Tools API no Google Cloud e defina "
            "google_fact_check_api_key (ou use a mesma chave do Gemini)."
        )

    if messenger:
        await messenger.step("Consultando verificadores de fact-check...")
    else:
        await pulse_whatsapp_typing()

    api_key = fact_check_api_key(settings)
    language = extract_fact_check_language(decision)

    try:
        claims, lang_used = await search_fact_checked_claims_with_fallback(
            http,
            api_key=api_key,
            query=query,
            language_code=language,
            page_size=5,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        log.warning("Fact-check API HTTP %s body=%s", status, exc.response.text[:300])
        if status in (401, 403):
            return (
                "Não consegui acessar o Google Fact Check Tools. "
                "Verifique se a API está ativada no Google Cloud e se a chave tem permissão."
            )
        return "Não consegui consultar o fact-check agora. Tente de novo em instantes."
    except Exception:
        log.exception("Fact-check search falhou query=%r", query[:80])
        return "Não consegui consultar o fact-check agora. Tente de novo em instantes."

    result = format_fact_check_response(query, claims, language_note=lang_used)
    result = f"{result}\n\n{format_fact_check_api_footer()}"
    log.info(
        "Fact-check phone query=%r claims=%s reviews_shown=%s",
        query[:60],
        len(claims),
        result.count("\n\n"),
    )
    return result
