"""Testes de migracao de /data/shakira_users para pasta no HA."""

from __future__ import annotations

from pathlib import Path

from app.user_data_migration import merge_legacy_user_data, maybe_migrate_legacy_user_data


def test_migrate_copies_when_dest_empty(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    phone = legacy / "5511999999999"
    phone.mkdir(parents=True)
    (phone / "birthdays.json").write_text('[{"id":"1"}]', encoding="utf-8")

    migrated = merge_legacy_user_data(legacy, dest)
    assert migrated == ["5511999999999"]
    assert (dest / "5511999999999" / "birthdays.json").read_text(encoding="utf-8") == '[{"id":"1"}]'


def test_migrate_merges_missing_phone_when_dest_has_other(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    (legacy / "5511111111111").mkdir(parents=True)
    (legacy / "5511111111111" / "memories.json").write_text('{"a":1}', encoding="utf-8")
    (dest / "5522222222222").mkdir(parents=True)
    (dest / "5522222222222" / "memories.json").write_text("[]", encoding="utf-8")

    migrated = merge_legacy_user_data(legacy, dest)
    assert migrated == ["5511111111111"]
    assert (dest / "5511111111111" / "memories.json").is_file()


def test_migrate_overwrites_smaller_dest_file(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    phone = "5511999999999"
    (legacy / phone).mkdir(parents=True)
    (legacy / phone / "birthdays.json").write_text(
        '[{"id":"1","name":"Ana"}]', encoding="utf-8"
    )
    (dest / phone).mkdir(parents=True)
    (dest / phone / "birthdays.json").write_text("[]", encoding="utf-8")

    migrated = merge_legacy_user_data(legacy, dest)
    assert migrated == [phone]
    assert "Ana" in (dest / phone / "birthdays.json").read_text(encoding="utf-8")


def test_migrate_copies_files_subdir(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    phone = "5511999999999"
    (legacy / phone / "files").mkdir(parents=True)
    (legacy / phone / "files" / "doc.pdf").write_bytes(b"%PDF-1.4")
    (dest / phone).mkdir(parents=True)

    migrated = merge_legacy_user_data(legacy, dest)
    assert migrated == [phone]
    assert (dest / phone / "files" / "doc.pdf").is_file()


def test_migrate_skips_when_legacy_empty(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    legacy.mkdir()
    assert merge_legacy_user_data(legacy, dest) == []


def test_migrate_skips_same_path(tmp_path: Path) -> None:
    root = tmp_path / "same"
    (root / "5511999999999").mkdir(parents=True)
    (root / "5511999999999" / "a.txt").write_text("x", encoding="utf-8")
    assert merge_legacy_user_data(root, root) == []


def test_maybe_migrate_returns_bool(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    (legacy / "5511999999999").mkdir(parents=True)
    (legacy / "5511999999999" / "birthdays.json").write_text("[]", encoding="utf-8")
    assert maybe_migrate_legacy_user_data(legacy, dest) is True
    assert maybe_migrate_legacy_user_data(legacy, dest) is False
