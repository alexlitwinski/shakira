"""Testes override fact-check quando Gemini recusa."""

from __future__ import annotations

from app.config import AppSettings
from app.fact_check_overrides import (
    extract_fact_check_query_from_user_text,
    try_fact_check_decision_override,
)


def _settings(*, enabled: bool = True, api_key: str = "test-key") -> AppSettings:
    return AppSettings(
        supervisor_token="",
        ha_url="http://localhost:8123",
        evolution_base_url="http://localhost:8080",
        evolution_api_key="evo",
        gemini_api_key=api_key,
        evolution_instance="",
        devices_config_path="",
        gemini_cache_ttl_hours=24,
        ha_states_cache_sec=30,
        photoprism_url="",
        photoprism_token="",
        photoprism_max_photos=5,
        photoprism_api_prefix="",
        frigate_url="",
        frigate_cameras_config_path="",
        alerts_config_path="",
        shakira_api_token="",
        vault_master_key="",
        apify_api_token="",
        apify_instagram_actor="",
        instagram_links_fetch_enabled=True,
        google_fact_check_api_key="",
        fact_check_enabled=enabled,
        log_level="info",
    )


def test_extract_query_verdade_que():
    text = "Verifique se é verdade que vacina pode causar cancer"
    assert extract_fact_check_query_from_user_text(text) == "vacina pode causar cancer"


def test_extract_query_isso_verdade_multiline():
    text = "Isso é verdade?\n\nUma operação prendeu a influenciadora Deolane Bezerra."
    assert "Deolane Bezerra" in extract_fact_check_query_from_user_text(text)


def test_override_refusal_reply():
    decision = {
        "action": "reply",
        "response": (
            "Não tenho informações sobre isso. Sou um sistema de automação residencial "
            "e não tenho conhecimento médico para verificar essa informação."
        ),
    }
    fixed = try_fact_check_decision_override(
        decision,
        user_text="Verifique se é verdade que vacina pode causar cancer",
        settings=_settings(),
    )
    assert fixed["action"] == "fact_check_claim"
    assert fixed["fact_check_query"] == "vacina pode causar cancer"


def test_override_intent_even_without_refusal():
    decision = {
        "action": "reply",
        "response": "Vou verificar isso para você.",
    }
    fixed = try_fact_check_decision_override(
        decision,
        user_text="Isso é verdade? A Terra é plana.",
        settings=_settings(),
    )
    assert fixed["action"] == "fact_check_claim"
    assert "Terra" in fixed["fact_check_query"]


def test_skip_home_state_question():
    decision = {
        "action": "reply",
        "response": "Não tenho informações.",
    }
    fixed = try_fact_check_decision_override(
        decision,
        user_text="É verdade que a porta social está aberta?",
        settings=_settings(),
    )
    assert fixed["action"] == "reply"


def test_skip_when_disabled():
    decision = {"action": "reply", "response": "Não tenho informações."}
    fixed = try_fact_check_decision_override(
        decision,
        user_text="Verifique se é verdade que vacina causa cancer",
        settings=_settings(enabled=False, api_key=""),
    )
    assert fixed["action"] == "reply"
