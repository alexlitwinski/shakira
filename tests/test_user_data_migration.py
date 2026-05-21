"""Testes de migracao de /data/shakira_users para pasta no HA."""

from __future__ import annotations

from pathlib import Path

from app.user_data_migration import maybe_migrate_legacy_user_data


def test_migrate_copies_when_dest_empty(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    phone = legacy / "5511999999999"
    phone.mkdir(parents=True)
    (phone / "birthdays.json").write_text("[]", encoding="utf-8")

    assert maybe_migrate_legacy_user_data(legacy, dest) is True
    assert (dest / "5511999999999" / "birthdays.json").is_file()


def test_migrate_skips_when_dest_has_data(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    (legacy / "5511111111111").mkdir(parents=True)
    (legacy / "5511111111111" / "memories.json").write_text("[]", encoding="utf-8")
    (dest / "5522222222222").mkdir(parents=True)
    (dest / "5522222222222" / "memories.json").write_text("[]", encoding="utf-8")

    assert maybe_migrate_legacy_user_data(legacy, dest) is False
    assert not (dest / "5511111111111").exists()


def test_migrate_skips_when_legacy_empty(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    legacy.mkdir()
    assert maybe_migrate_legacy_user_data(legacy, dest) is False


def test_migrate_skips_same_path(tmp_path: Path) -> None:
    root = tmp_path / "same"
    (root / "5511999999999").mkdir(parents=True)
    (root / "5511999999999" / "a.txt").write_text("x", encoding="utf-8")
    assert maybe_migrate_legacy_user_data(root, root) is False
