"""Catalogo de dispositivos acionaveis (YAML em /config)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_DEVICES_PATH = "/config/shakira_devices.yaml"
# Em muitos add-ons HA OS a pasta de configuracao monta em /homeassistant, nao em /config
FALLBACK_DEVICES_PATHS = (
    "/homeassistant/shakira_devices.yaml",
    "/config/shakira_devices.yaml",
)

ALLOWED_ROOT_KEYS = frozenset({"devices", "scenarios"})
ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]+\.[a-z0-9_]+$", re.IGNORECASE)
SCENARIO_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$", re.IGNORECASE)


class CatalogValidationError(ValueError):
    """Erros de estrutura do shakira_devices.yaml."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def resolve_devices_path(configured: str | Path | None = None) -> Path:
    """Resolve o YAML de dispositivos (caminho configurado ou fallbacks do Supervisor)."""
    candidates: list[Path] = []
    if configured and str(configured).strip():
        candidates.append(Path(str(configured).strip()))
    env = os.environ.get("SHAKIRA_DEVICES_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    for p in FALLBACK_DEVICES_PATHS:
        candidates.append(Path(p))
    candidates.append(Path(DEFAULT_DEVICES_PATH))

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            if configured and key != str(Path(str(configured).strip())):
                log.info("Usando catalogo em %s (caminho configurado nao encontrado)", path)
            return path

    return Path(str(configured).strip()) if configured and str(configured).strip() else Path(DEFAULT_DEVICES_PATH)


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
    password_prompt: str = "Informe a senha para confirmar esta ação."


@dataclass
class EntityConfig:
    entity_id: str
    description: str = ""
    allow_actions: bool = False
    security: SecurityConfig | None = None
    service_defaults: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceConfig:
    name: str
    entities: list[EntityConfig] = field(default_factory=list)


@dataclass
class ScenarioConfig:
    """Instrucao em linguagem natural para o Gemini (definida no YAML)."""

    id: str
    prompt: str


@dataclass
class DevicesCatalog:
    devices: list[DeviceConfig] = field(default_factory=list)
    scenarios: list[ScenarioConfig] = field(default_factory=list)
    source_path: Path | None = None
    content_hash: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> DevicesCatalog:
        resolved = resolve_devices_path(path)
        if not resolved.is_file():
            log.warning("Arquivo de dispositivos NAO encontrado: %s", resolved)
            _log_config_dir_hint(resolved)
            return cls(devices=[], source_path=resolved, content_hash="")

        raw_bytes = resolved.read_bytes()
        try:
            return cls.from_yaml_string(
                raw_bytes.decode("utf-8"),
                source_path=resolved,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            )
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            log.error("YAML invalido em %s: %s", resolved, e)
            return cls(devices=[], source_path=resolved, content_hash=hashlib.sha256(raw_bytes).hexdigest())

    @classmethod
    def from_yaml_string(
        cls,
        text: str,
        *,
        source_path: Path | None = None,
        content_hash: str | None = None,
        strict_scenarios: bool = False,
    ) -> DevicesCatalog:
        h = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        data = yaml.safe_load(text)
        devices, scenarios = cls._parse_data(data, strict_scenarios=strict_scenarios)
        actionable = sum(1 for d in devices for e in d.entities if e.allow_actions)
        if source_path:
            log.info(
                "Catalogo carregado: %s (%s dispositivos, %s entidades, %s acionaveis, %s cenarios)",
                source_path,
                len(devices),
                sum(len(d.entities) for d in devices),
                actionable,
                len(scenarios),
            )
        return cls(
            devices=devices,
            scenarios=scenarios,
            source_path=source_path,
            content_hash=h,
        )

    @staticmethod
    def _parse_data(
        data: Any,
        *,
        strict_scenarios: bool = False,
    ) -> tuple[list[DeviceConfig], list[ScenarioConfig]]:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("O YAML deve ser um mapa na raiz (devices:, scenarios:).")

        if "devices" in data and not isinstance(data["devices"], list):
            raise ValueError("devices deve ser uma lista.")
        if "scenarios" in data and not isinstance(data["scenarios"], list):
            raise ValueError("scenarios deve ser uma lista.")

        devices: list[DeviceConfig] = []
        for dev in data.get("devices") or []:
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
                        or "Informe a senha para confirmar esta ação.",
                    )
                defaults: dict[str, Any] = {}
                raw_defaults = ent.get("service_defaults")
                if isinstance(raw_defaults, dict):
                    defaults = {
                        str(k): v for k, v in raw_defaults.items() if str(k).strip()
                    }
                entities.append(
                    EntityConfig(
                        entity_id=eid,
                        description=str(ent.get("description") or "").strip(),
                        allow_actions=bool(ent.get("allow_actions", False)),
                        security=sec,
                        service_defaults=defaults,
                    )
                )
            devices.append(DeviceConfig(name=name, entities=entities))

        scenarios: list[ScenarioConfig] = []
        for i, row in enumerate(data.get("scenarios") or []):
            if not isinstance(row, dict):
                if strict_scenarios:
                    raise ValueError(f"Cenario #{i + 1} invalido.")
                continue
            sid = str(row.get("id") or "").strip()
            prompt = str(row.get("prompt") or "").strip()
            if not sid or not prompt:
                if strict_scenarios:
                    raise ValueError(f"Cenario #{i + 1} precisa de id e prompt.")
                log.warning("Cenario ignorado (falta id ou prompt): %s", row)
                continue
            scenarios.append(ScenarioConfig(id=sid, prompt=prompt))

        return devices, scenarios

    @staticmethod
    def validate_structure(data: Any) -> list[str]:
        """Valida estrutura do YAML antes de gravar. Retorna lista de erros (vazia = OK)."""
        errors: list[str] = []

        if data is None:
            return ["Documento vazio. Defina ao menos 'devices:' ou 'scenarios:'."]
        if not isinstance(data, dict):
            return ["A raiz do arquivo deve ser um mapa YAML (chave: valor), nao lista ou texto solto."]

        for key in sorted(set(data.keys()) - ALLOWED_ROOT_KEYS):
            errors.append(f"Chave invalida na raiz: '{key}' (permitido: devices, scenarios).")

        if "devices" not in data and "scenarios" not in data:
            errors.append("Defina ao menos uma secao: 'devices:' ou 'scenarios:'.")

        if "devices" in data:
            if not isinstance(data["devices"], list):
                errors.append("'devices' deve ser uma lista.")
            else:
                seen_entities: set[str] = set()
                for i, dev in enumerate(data["devices"]):
                    path = f"devices[{i}]"
                    if not isinstance(dev, dict):
                        errors.append(f"{path}: cada dispositivo deve ser um mapa (name, entities).")
                        continue
                    name = dev.get("name")
                    if not isinstance(name, str) or not name.strip():
                        errors.append(f"{path}: campo 'name' obrigatorio (texto).")
                    entities = dev.get("entities")
                    if entities is None:
                        errors.append(f"{path}: falta lista 'entities:'.")
                        continue
                    if not isinstance(entities, list):
                        errors.append(f"{path}.entities: deve ser uma lista.")
                        continue
                    if not entities:
                        errors.append(f"{path}: 'entities' nao pode estar vazia.")
                    for j, ent in enumerate(entities):
                        ep = f"{path}.entities[{j}]"
                        if not isinstance(ent, dict):
                            errors.append(f"{ep}: cada entidade deve ser um mapa.")
                            continue
                        eid = ent.get("entity_id")
                        if not isinstance(eid, str) or not eid.strip():
                            errors.append(f"{ep}: 'entity_id' obrigatorio.")
                            continue
                        eid = eid.strip()
                        if not ENTITY_ID_RE.match(eid):
                            errors.append(
                                f"{ep}: entity_id invalido '{eid}' (use formato dominio.nome, ex.: light.sala)."
                            )
                        elif eid in seen_entities:
                            errors.append(f"{ep}: entity_id duplicado '{eid}'.")
                        else:
                            seen_entities.add(eid)
                        if "allow_actions" in ent and not isinstance(ent["allow_actions"], bool):
                            errors.append(f"{ep}: 'allow_actions' deve ser true ou false.")
                        sec = ent.get("security")
                        if sec is not None and not isinstance(sec, dict):
                            errors.append(f"{ep}: 'security' deve ser um mapa.")
                        elif isinstance(sec, dict):
                            rps = sec.get("require_password_for_services")
                            if rps is not None and not isinstance(rps, list):
                                errors.append(
                                    f"{ep}.security: 'require_password_for_services' deve ser lista."
                                )
                        sd = ent.get("service_defaults")
                        if sd is not None and not isinstance(sd, dict):
                            errors.append(f"{ep}: 'service_defaults' deve ser um mapa.")

        if "scenarios" in data:
            if not isinstance(data["scenarios"], list):
                errors.append("'scenarios' deve ser uma lista.")
            else:
                seen_ids: set[str] = set()
                for i, row in enumerate(data["scenarios"]):
                    path = f"scenarios[{i}]"
                    if not isinstance(row, dict):
                        errors.append(f"{path}: cada cenario deve ser um mapa (id, prompt).")
                        continue
                    sid = row.get("id")
                    if not isinstance(sid, str) or not sid.strip():
                        errors.append(f"{path}: 'id' obrigatorio.")
                    else:
                        sid = sid.strip()
                        if not SCENARIO_ID_RE.match(sid):
                            errors.append(
                                f"{path}: id invalido '{sid}' (use letras, numeros e _)."
                            )
                        elif sid in seen_ids:
                            errors.append(f"{path}: id duplicado '{sid}'.")
                        else:
                            seen_ids.add(sid)
                    prompt = row.get("prompt")
                    if not isinstance(prompt, str) or not prompt.strip():
                        errors.append(f"{path}: 'prompt' obrigatorio (texto com instrucoes).")
                    extra = set(row.keys()) - {"id", "prompt"}
                    for k in sorted(extra):
                        errors.append(f"{path}: chave desconhecida '{k}' (use id e prompt).")

        return errors

    def entity_map(self) -> dict[str, EntityConfig]:
        out: dict[str, EntityConfig] = {}
        for dev in self.devices:
            for ent in dev.entities:
                out[ent.entity_id] = ent
        return out

    def actionable_entity_ids(self) -> set[str]:
        return {eid for eid, ent in self.entity_map().items() if ent.allow_actions}

    def context_entity_ids(self) -> list[str]:
        """Entidades cujo estado entra no prompt Gemini (devices + citadas nos cenarios)."""
        seen: set[str] = set()
        out: list[str] = []
        for eid in sorted(self.entity_map().keys()):
            seen.add(eid)
            out.append(eid)
        from app.scenario_context import entity_ids_for_scenarios

        for eid in entity_ids_for_scenarios(self.scenarios):
            if eid not in seen:
                seen.add(eid)
                out.append(eid)
        return out

    def get_entity(self, entity_id: str) -> EntityConfig | None:
        return self.entity_map().get(entity_id)

    def apply_service_defaults(self, entity_id: str, service_data: dict[str, Any]) -> dict[str, Any]:
        ent = self.get_entity(entity_id)
        if not ent or not ent.service_defaults:
            return service_data
        merged = dict(service_data)
        merged.update(ent.service_defaults)
        return merged

    def build_catalog_context(self) -> str:
        lines: list[str] = [
            "CATALOGO DE DISPOSITIVOS (consulta e acoes permitidas):",
            "- [somente contexto] = pode ler estado e explicar ao usuario",
            "- [ACIONAVEL] = pode usar call_service para alterar",
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
        if self.scenarios:
            lines.append("")
            lines.append("CENARIOS (siga estas instrucoes quando a mensagem do usuario se aplicar):")
            for sc in self.scenarios:
                lines.append(f"  [{sc.id}]")
                lines.append(f"    {sc.prompt}")
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
        return "Informe a senha para confirmar esta ação."
