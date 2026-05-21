"""Testes de busca em perfis Instagram guardados."""

from __future__ import annotations

from app.instagram_links_actions import (
    format_instagram_links_list,
    is_search_instagram_profiles_intent,
    search_instagram_profiles,
)
from app.instagram_links_store import get_instagram_store


def test_list_without_id(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    store = get_instagram_store("5511999999999")
    ent = store.create_draft(url="https://www.instagram.com/foo/", handle="foo")
    store.mark_saved(ent.id, user_note="restaurante")
    text = format_instagram_links_list("5511999999999")
    assert "@foo" in text
    assert "id=" not in text


def test_search_intent():
    assert is_search_instagram_profiles_intent("Quero um perfil que fale sobre IA")
    assert not is_search_instagram_profiles_intent("buscar fotos sobre IA")


def test_search_by_bio(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAKIRA_USER_DATA_ROOT", str(tmp_path))
    store = get_instagram_store("5511888888888")
    ent = store.create_draft(url="https://www.instagram.com/ai_guru/", handle="ai_guru")
    ent.profile_bio = "Inteligencia artificial e machine learning"
    ent.save_status = "saved"
    store.update_entry(ent)
    result = search_instagram_profiles("5511888888888", "Quero um perfil que fale sobre IA")
    assert "@ai_guru" in result
    assert "Inteligencia" in result
