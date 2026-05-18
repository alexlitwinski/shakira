"""Catalogo de dispositivos acionaveis (YAML em /config)."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_DEVICES_PATH = "/config/shakira_devices.yaml"


def _log_config_dir_hint(target: Path) -> None:
    """Ajuda a diagnosticar ficheiro no sítio errado."""
    for parent in (target.parent, Path("/config"), Path("/homeassistant")):
        if not parent.is_dir():
            continue
        try:
            names = sorted(p.name for p in parent.iterdir() if p.is_file())[:25]
            log.warning("Ficheiros em %s: %s", parent, names or "(vazio)")
        except OSError:
            pass


@dataclass
class SecurityConfig:
    require_password_for_services: list[str] = field(default_factory=list)
    password: str = ""
    password_prompt: str = "Informe a senha para confirmar esta acao."


@dataclass
class EntityConfig:
    entity_id: str
    description: str = ""
    allow_actions: bool = False
    security: SecurityConfig | None = None


@dataclass
class DeviceConfig:
    name: str
    entities: list[EntityConfig] = field(default_factory=list)


@dataclass
class DevicesCatalog:
    devices: list[DeviceConfig] = field(default_factory=list)
    source_path: Path | None = None
    content_hash: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> DevicesCatalog:
        resolved = Path(path or os.environ.get("SHAKIRA_DEVICES_PATH", DEFAULT_DEVICES_PATH))
        if not resolved.is_file():
            log.warning("Arquivo de dispositivos NAO encontrado: %s", resolved)
            _log_config_dir_hint(resolved)
            return cls(devices=[], source_path=resolved, content_hash="")

        raw_bytes = resolved.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        try:
            data = yaml.safe_load(raw_bytes.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            log.error("YAML invalido em %s: %s", resolved, e)
            return cls(devices=[], source_path=resolved, content_hash=content_hash)

        devices: list[DeviceConfig] = []
        if isinstance(data, dict) and isinstance(data.get("devices"), list):
            for dev in data["devices"]:
                if not isinstance(dev, dict):
                    continue
                name = str(dev.get("name") or "Dispositivo").strip()
                entities: list[EntityConfig] = []
                for ent in dev.get("entities") or []:
                    if not isinstance(ent, dict):
                        continue
                    eid = str(ent.get("entity_id") or "").strip()
                    if not eid:
                        continue
                    sec = None
                    if isinstance(ent.get("security"), dict):
                        s = ent["security"]
                        rps = s.get("require_password_for_services") or []
                        if not isinstance(rps, list):
                            rps = [str(rps)]
                        sec = SecurityConfig(
                            require_password_for_services=[str(x).strip() for x in rps if str(x).strip()],
                            password=str(s.get("password") or ""),
                            password_prompt=str(s.get("password_prompt") or "").strip()
                            or "Informe a senha para confirmar esta acao.",
                        )
                    entities.append(
                        EntityConfig(
                            entity_id=eid,
                            description=str(ent.get("description") or "").strip(),
                            allow_actions=bool(ent.get("allow_actions", False)),
                            security=sec,
                        )
                    )
                devices.append(DeviceConfig(name=name, entities=entities))

        actionable = sum(1 for d in devices for e in d.entities if e.allow_actions)
        log.info(
            "Catalogo carregado: %s (%s dispositivos, %s entidades, %s acionaveis)",
            resolved,
            len(devices),
            sum(len(d.entities) for d in devices),
            actionable,
        )
        return cls(devices=devices, source_path=resolved, content_hash=content_hash)

    def entity_map(self) -> dict[str, EntityConfig]:
        out: dict[str, EntityConfig] = {}
        for dev in self.devices:
            for ent in dev.entities:
                out[ent.entity_id] = ent
        return out

    def actionable_entity_ids(self) -> set[str]:
        return {eid for eid, ent in self.entity_map().items() if ent.allow_actions}

    def get_entity(self, entity_id: str) -> EntityConfig | None:
        return self.entity_map().get(entity_id)

    def build_catalog_context(self) -> str:
        lines: list[str] = [
            "CATALOGO DE DISPOSITIVOS (unicas entidades que podem ser ALTERADAS via call_service):",
            "",
        ]
        for dev in self.devices:
            lines.append(f"dispositivo: {dev.name}")
            for ent in dev.entities:
                flag = "ACIONAVEL" if ent.allow_actions else "somente contexto"
                lines.append(f"  - {ent.entity_id} [{flag}]")
                if ent.description:
                    lines.append(f"    {ent.description}")
                if ent.security and ent.security.require_password_for_services:
                    svcs = ", ".join(ent.security.require_password_for_services)
                    lines.append(
                        f"    Seguranca: servico(s) {svcs} exigem senha antes de executar. "
                        f"Pergunte ao usuario: {ent.security.password_prompt}"
                    )
            lines.append("")
        if not self.devices:
            lines.append("(Nenhum dispositivo configurado - nenhuma acao permitida.)")
        return "\n".join(lines)

    def service_requires_password(self, entity_id: str, service: str) -> bool:
        ent = self.get_entity(entity_id)
        if not ent or not ent.security:
            return False
        return service.strip().lower() in {s.lower() for s in ent.security.require_password_for_services}

    def verify_password(self, entity_id: str, candidate: str, settings_password_override: str = "") -> bool:
        ent = self.get_entity(entity_id)
        if not ent or not ent.security:
            return True
        expected = settings_password_override.strip() or ent.security.password.strip()
        if not expected:
            return False
        return candidate.strip() == expected

    def password_prompt_for(self, entity_id: str) -> str:
        ent = self.get_entity(entity_id)
        if ent and ent.security and ent.security.password_prompt:
            return ent.security.password_prompt
        return "Informe a senha para confirmar esta acao."
