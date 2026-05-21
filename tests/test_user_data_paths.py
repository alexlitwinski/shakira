"""Testes de resolucao e configuracao da pasta de dados por utilizador."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import user_memory
from app.user_memory import configure_user_data_root, resolve_user_data_root


def test_resolve_uses_configured_path(tmp_path: Path) -> None:
    custom = tmp_path / "my_users"
    custom.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_user_data_root(str(custom))
    assert resolved == custom
    assert custom.is_dir()


def test_resolve_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("SHAKIRA_USER_DATA_PATH", str(env_dir))
    resolved = resolve_user_data_root(None)
    assert resolved == env_dir


def test_configure_updates_module_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "configured"
    monkeypatch.delenv("SHAKIRA_USER_DATA_ROOT", raising=False)
    configure_user_data_root(target)
    assert user_memory.USER_DATA_ROOT == target
    assert target.is_dir()
    assert __import__("os").environ.get("SHAKIRA_USER_DATA_ROOT") == str(target)
