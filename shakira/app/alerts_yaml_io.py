"""Leitura e gravacao de shakira_alerts.yaml no painel do add-on."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.alerts_catalog import (
    AlertsCatalog,
    AlertsCatalogValidationError,
    resolve_alerts_path,
)

log = logging.getLogger(__name__)

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "shakira_alerts.example.yaml"


def _default_template() -> str:
    if _EXAMPLE_PATH.is_file():
        return _EXAMPLE_PATH.read_text(encoding="utf-8")
    return """alerts:
  - id: cameras_paradas
    enabled: true
    check_interval: 5m
    entity_id: binary_sensor.status_cameras_paradas
    when_state: "on"
    message: "Atencao: existem cameras do sistema com problema."
    cooldown: 1h
    notify:
      phones: []
"""


def _parse_yaml(content: str) -> Any:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML invalido: {e}") from e


def validate_yaml_content(content: str) -> list[str]:
    data = _parse_yaml(content)
    return AlertsCatalog.validate_structure(data)


def read_yaml_file(configured_path: str) -> dict[str, Any]:
    path = resolve_alerts_path(configured_path)
    exists = path.is_file()
    content = path.read_text(encoding="utf-8") if exists else _default_template()

    validation_errors: list[str] = []
    if exists:
        try:
            validation_errors = validate_yaml_content(content)
        except ValueError as e:
            validation_errors = [str(e)]

    if exists and not validation_errors:
        catalog = AlertsCatalog.load(str(path))
    else:
        catalog = AlertsCatalog(alerts=[], source_path=path)

    enabled = sum(1 for a in catalog.alerts if a.enabled)
    return {
        "path": str(path),
        "configured_path": configured_path,
        "exists": exists,
        "content": content,
        "valid": not validation_errors,
        "validation_errors": validation_errors,
        "alerts_count": len(catalog.alerts),
        "enabled_count": enabled,
    }


def write_yaml_file(configured_path: str, content: str) -> dict[str, Any]:
    data = _parse_yaml(content)
    errors = AlertsCatalog.validate_structure(data)
    if errors:
        raise AlertsCatalogValidationError(errors)

    catalog = AlertsCatalog.from_yaml_string(
        content,
        source_path=resolve_alerts_path(configured_path),
    )

    path = resolve_alerts_path(configured_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("shakira_alerts.yaml gravado: %s", path)

    enabled = sum(1 for a in catalog.alerts if a.enabled)
    return {
        "ok": True,
        "path": str(path),
        "valid": True,
        "alerts_count": len(catalog.alerts),
        "enabled_count": enabled,
    }
