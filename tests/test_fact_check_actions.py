"""Testes formatacao fact-check."""

from __future__ import annotations

from app.fact_check_actions import (
    extract_fact_check_query,
    format_fact_check_api_footer,
    format_fact_check_response,
)


def test_format_empty_results():
    text = format_fact_check_response("vacina causa autismo", [])
    assert "Não encontrei" in text
    assert "vacina causa autismo" in text


def test_format_with_reviews():
    claims = [
        {
            "text": "A Terra e plana",
            "claimant": "Blog X",
            "claimReview": [
                {
                    "textualRating": "False",
                    "title": "Checamos a alegacao",
                    "url": "https://example.com/fact",
                    "reviewDate": "2024-06-01T12:00:00Z",
                    "publisher": {"name": "Agencia Fato", "site": "agenciafato.com"},
                }
            ],
        }
    ]
    text = format_fact_check_response("Terra plana", claims)
    assert "Falso" in text
    assert "Agencia Fato" in text
    assert "https://example.com/fact" in text


def test_extract_query_from_decision():
    decision = {"fact_check_query": "  Bolsonaro preso em 2024  "}
    assert extract_fact_check_query(decision) == "Bolsonaro preso em 2024"


def test_api_footer_timestamp():
    from datetime import datetime, timezone

    when = datetime(2026, 5, 21, 11, 48, tzinfo=timezone.utc)
    footer = format_fact_check_api_footer(when=when)
    assert "✓ Consulta à API Google Fact Check concluída às 08:48" in footer
    assert "Brasília" in footer
