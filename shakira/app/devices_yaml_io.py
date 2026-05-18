"""Leitura e gravacao de shakira_devices.yaml no painel do add-on."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from app.devices_catalog import (
    CatalogValidationError,
    DevicesCatalog,
    resolve_devices_path,
)

log = logging.getLogger(__name__)

DEFAULT_TEMPLATE = """# Catalogo Shakira — dispositivos e cenarios

devices:
  - name: Boiler
    entities:
      - entity_id: sensor.temperatura_boiler
        description: Temperatura da agua do boiler
        allow_actions: false
      - entity_id: input_select.modo_do_boiler
        description: Modo do boiler
        allow_actions: true

scenarios:
  - id: banho_boiler
    prompt: >
      Se o usuario perguntar se pode tomar banho ou se a agua do boiler esta quente,
      verifique sensor.temperatura_boiler. Abaixo de 42 graus C, pergunte se deve
      aquecer; se confirmar, ligue input_select.modo_do_boiler (option Ligado).
"""


def _parse_yaml(content: str) -> Any:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML invalido: {e}") from e


def validate_yaml_content(content: str) -> list[str]:
    """Valida sintaxe e estrutura sem gravar."""
    data = _parse_yaml(content)
    return DevicesCatalog.validate_structure(data)


def read_yaml_file(configured_path: str) -> dict[str, Any]:
    path = resolve_devices_path(configured_path)
    exists = path.is_file()
    content = path.read_text(encoding="utf-8") if exists else DEFAULT_TEMPLATE

    validation_errors: list[str] = []
    if exists:
        try:
            validation_errors = validate_yaml_content(content)
        except ValueError as e:
            validation_errors = [str(e)]

    if exists and not validation_errors:
        catalog = DevicesCatalog.load(str(path))
    else:
        catalog = DevicesCatalog(devices=[], scenarios=[], source_path=path)

    return {
        "path": str(path),
        "configured_path": configured_path,
        "exists": exists,
        "content": content,
        "valid": not validation_errors,
        "validation_errors": validation_errors,
        "devices_count": len(catalog.devices),
        "scenarios_count": len(catalog.scenarios),
        "actionable_count": len(catalog.actionable_entity_ids()),
    }


def write_yaml_file(configured_path: str, content: str) -> dict[str, Any]:
    data = _parse_yaml(content)
    errors = DevicesCatalog.validate_structure(data)
    if errors:
        raise CatalogValidationError(errors)

    catalog = DevicesCatalog.from_yaml_string(
        content,
        source_path=resolve_devices_path(configured_path),
        strict_scenarios=True,
    )

    path = resolve_devices_path(configured_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("shakira_devices.yaml gravado: %s", path)

    return {
        "ok": True,
        "path": str(path),
        "valid": True,
        "devices_count": len(catalog.devices),
        "scenarios_count": len(catalog.scenarios),
        "actionable_count": len(catalog.actionable_entity_ids()),
    }
