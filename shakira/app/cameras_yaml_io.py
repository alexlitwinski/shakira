"""Leitura e gravacao de shakira_cameras.yaml no painel do add-on."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.cameras_catalog import (
    CamerasCatalog,
    CamerasCatalogValidationError,
    resolve_cameras_path,
)

log = logging.getLogger(__name__)

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "shakira_cameras.example.yaml"


def _default_template() -> str:
    if _EXAMPLE_PATH.is_file():
        return _EXAMPLE_PATH.read_text(encoding="utf-8")
    return """cameras:
  - id: Cozinha
    name: Cozinha
    description: Cozinha — deteccao de pessoa
"""


def _parse_yaml(content: str) -> Any:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML invalido: {e}") from e


def validate_yaml_content(content: str) -> list[str]:
    """Valida sintaxe e estrutura sem gravar."""
    data = _parse_yaml(content)
    return CamerasCatalog.validate_structure(data)


def read_yaml_file(configured_path: str) -> dict[str, Any]:
    path = resolve_cameras_path(configured_path)
    exists = path.is_file()
    content = path.read_text(encoding="utf-8") if exists else _default_template()

    validation_errors: list[str] = []
    if exists:
        try:
            validation_errors = validate_yaml_content(content)
        except ValueError as e:
            validation_errors = [str(e)]

    if exists and not validation_errors:
        catalog = CamerasCatalog.load(str(path))
    else:
        catalog = CamerasCatalog(cameras=[], source_path=path)

    return {
        "path": str(path),
        "configured_path": configured_path,
        "exists": exists,
        "content": content,
        "valid": not validation_errors,
        "validation_errors": validation_errors,
        "cameras_count": len(catalog.cameras),
    }


def write_yaml_file(configured_path: str, content: str) -> dict[str, Any]:
    data = _parse_yaml(content)
    errors = CamerasCatalog.validate_structure(data)
    if errors:
        raise CamerasCatalogValidationError(errors)

    catalog = CamerasCatalog.from_yaml_string(
        content,
        source_path=resolve_cameras_path(configured_path),
    )

    path = resolve_cameras_path(configured_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("shakira_cameras.yaml gravado: %s", path)

    return {
        "ok": True,
        "path": str(path),
        "valid": True,
        "cameras_count": len(catalog.cameras),
    }
