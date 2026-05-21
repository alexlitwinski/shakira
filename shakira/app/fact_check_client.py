"""Cliente Google Fact Check Tools API (claims:search)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

FACT_CHECK_BASE = "https://factchecktools.googleapis.com/v1alpha1"
_SEARCH_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


async def search_fact_checked_claims(
    http: httpx.AsyncClient,
    *,
    api_key: str,
    query: str,
    language_code: str | None = "pt-BR",
    page_size: int = 5,
    max_age_days: int | None = None,
) -> list[dict[str, Any]]:
    """Busca alegacoes verificadas na base do Google Fact Check Tools."""
    key = (api_key or "").strip()
    q = (query or "").strip()
    if not key:
        raise ValueError("Chave da API Google Fact Check nao configurada")
    if not q:
        raise ValueError("Consulta de fact-check vazia")

    params: dict[str, Any] = {
        "query": q,
        "key": key,
        "pageSize": max(1, min(page_size, 10)),
    }
    if language_code:
        params["languageCode"] = language_code.strip()
    if max_age_days is not None and max_age_days > 0:
        params["maxAgeDays"] = int(max_age_days)

    url = f"{FACT_CHECK_BASE}/claims:search"
    log.info("Fact-check search query=%r lang=%s", q[:80], language_code or "(any)")
    resp = await http.get(url, params=params, timeout=_SEARCH_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return []
    claims = data.get("claims")
    if not isinstance(claims, list):
        return []
    return [x for x in claims if isinstance(x, dict)]


async def search_fact_checked_claims_with_fallback(
    http: httpx.AsyncClient,
    *,
    api_key: str,
    query: str,
    language_code: str = "pt-BR",
    page_size: int = 5,
) -> tuple[list[dict[str, Any]], str]:
    """
    Tenta pt-BR (ou idioma pedido) e, se vazio, repete sem filtro de idioma.
    Retorna (claims, idioma_efetivo).
    """
    primary = (language_code or "").strip() or "pt-BR"
    claims = await search_fact_checked_claims(
        http,
        api_key=api_key,
        query=query,
        language_code=primary,
        page_size=page_size,
    )
    if claims:
        return claims, primary

    if primary.casefold() not in ("", "pt", "pt-br"):
        claims = await search_fact_checked_claims(
            http,
            api_key=api_key,
            query=query,
            language_code="pt-BR",
            page_size=page_size,
        )
        if claims:
            return claims, "pt-BR"

    claims = await search_fact_checked_claims(
        http,
        api_key=api_key,
        query=query,
        language_code=None,
        page_size=page_size,
    )
    return claims, "qualquer idioma"
