"""Migracao de dados de utilizador do volume legado /data para /config."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_USER_DATA_ROOT = Path("/data/shakira_users")


def _dir_has_user_entries(root: Path) -> bool:
    if not root.is_dir():
        return False
    try:
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() or child.is_file():
                return True
    except OSError:
        return False
    return False


def maybe_migrate_legacy_user_data(
    legacy: Path,
    dest: Path,
) -> bool:
    """
    Copia shakira_users de /data para o destino no HA se o destino estiver vazio.

    Nao apaga o legado. Retorna True se a migracao correu com sucesso.
    """
    legacy = Path(legacy)
    dest = Path(dest)
    try:
        if legacy.resolve() == dest.resolve():
            return False
    except OSError:
        if str(legacy) == str(dest):
            return False

    if not _dir_has_user_entries(legacy):
        return False
    if _dir_has_user_entries(dest):
        log.info(
            "Dados de utilizador ja existem em %s; migracao de %s ignorada",
            dest,
            legacy,
        )
        return False

    try:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, dest, dirs_exist_ok=True)
        log.info(
            "Dados de utilizador migrados de %s para %s (pode apagar o legado manualmente)",
            legacy,
            dest,
        )
        return True
    except OSError as exc:
        log.warning(
            "Falha ao migrar dados de %s para %s: %s",
            legacy,
            dest,
            exc,
        )
        return False
