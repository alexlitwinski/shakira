"""Migracao de dados de utilizador do volume legado /data para /config."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_USER_DATA_ROOT = Path("/data/shakira_users")

_USER_DATA_FILENAMES = frozenset(
    {
        "birthdays.json",
        "memories.json",
        "files_manifest.json",
        "scheduled_responses.json",
        "google_calendar.json",
        "instagram_links.json",
        "vault.enc.json",
        "gemini_memory_cache.json",
        "pending_file.json",
    }
)


def _is_phone_dir(name: str) -> bool:
    return name.isdigit() and len(name) >= 8


def _paths_equal(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _list_phone_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for child in root.iterdir():
            if child.is_dir() and _is_phone_dir(child.name):
                out.append(child)
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name)


def _should_copy_file(src: Path, dst: Path) -> bool:
    if not dst.is_file():
        return True
    try:
        return src.stat().st_size > dst.stat().st_size
    except OSError:
        return True


def _merge_phone_dir(legacy_phone: Path, dest_phone: Path) -> bool:
    """Copia ficheiros em falta ou mais recentes/maiores do legado para o destino."""
    changed = False
    dest_phone.mkdir(parents=True, exist_ok=True)

    def _copy_one(src: Path, dst: Path) -> None:
        nonlocal changed
        if not _should_copy_file(src, dst):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        changed = True

    for name in _USER_DATA_FILENAMES:
        src = legacy_phone / name
        if src.is_file():
            _copy_one(src, dest_phone / name)

    legacy_files = legacy_phone / "files"
    if legacy_files.is_dir():
        dest_files = dest_phone / "files"
        dest_files.mkdir(parents=True, exist_ok=True)
        try:
            for src in legacy_files.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(legacy_files)
                _copy_one(src, dest_files / rel)
        except OSError as exc:
            log.warning("Falha ao copiar files/ de %s: %s", legacy_phone.name, exc)

    legacy_pending = legacy_phone / "pending"
    if legacy_pending.is_dir():
        dest_pending = dest_phone / "pending"
        dest_pending.mkdir(parents=True, exist_ok=True)
        try:
            for src in legacy_pending.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(legacy_pending)
                _copy_one(src, dest_pending / rel)
        except OSError as exc:
            log.warning("Falha ao copiar pending/ de %s: %s", legacy_phone.name, exc)

    return changed


def merge_legacy_user_data(legacy: Path, dest: Path) -> list[str]:
    """
    Funde dados do legado no destino, por pasta de telefone.

    Copia ficheiros que nao existem no destino ou sao maiores no legado.
    Retorna lista de telefones com pelo menos um ficheiro copiado.
    """
    legacy = Path(legacy)
    dest = Path(dest)
    if _paths_equal(legacy, dest):
        return []

    dest.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []

    for legacy_phone in _list_phone_dirs(legacy):
        dest_phone = dest / legacy_phone.name
        try:
            if _merge_phone_dir(legacy_phone, dest_phone):
                migrated.append(legacy_phone.name)
        except OSError as exc:
            log.warning(
                "Falha ao migrar dados do telefone %s de %s para %s: %s",
                legacy_phone.name,
                legacy,
                dest,
                exc,
            )

    if migrated:
        log.info(
            "Dados de utilizador migrados de %s para %s (telefones: %s). "
            "Pode apagar o legado em %s quando confirmar.",
            legacy,
            dest,
            ", ".join(migrated),
            legacy,
        )
    else:
        legacy_phones = [p.name for p in _list_phone_dirs(legacy)]
        dest_phones = [p.name for p in _list_phone_dirs(dest)]
        if legacy_phones:
            log.info(
                "Migracao legado->destino: nada novo a copiar (legado=%s, destino=%s)",
                legacy_phones,
                dest_phones or "(vazio)",
            )

    return migrated


def maybe_migrate_legacy_user_data(legacy: Path, dest: Path) -> bool:
    """Migra dados do legado; retorna True se copiou pelo menos um telefone."""
    return bool(merge_legacy_user_data(legacy, dest))
