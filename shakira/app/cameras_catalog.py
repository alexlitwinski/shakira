"""Catalogo de cameras Frigate (YAML em /config)."""

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

DEFAULT_CAMERAS_PATH = "/config/shakira_cameras.yaml"
FALLBACK_CAMERAS_PATHS = (
    "/homeassistant/shakira_cameras.yaml",
    "/config/shakira_cameras.yaml",
)

CAMERA_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
ALLOWED_ROOT_KEYS = frozenset({"cameras"})


class CamerasCatalogValidationError(ValueError):
    """Erros de estrutura do shakira_cameras.yaml."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class CameraConfig:
    id: str
    name: str = ""
    description: str = ""


@dataclass
class CamerasCatalog:
    cameras: list[CameraConfig] = field(default_factory=list)
    source_path: Path | None = None
    content_hash: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> CamerasCatalog:
        resolved = resolve_cameras_path(path)
        if not resolved.is_file():
            log.warning("Arquivo de cameras NAO encontrado: %s", resolved)
            return cls(cameras=[], source_path=resolved, content_hash="")

        raw_bytes = resolved.read_bytes()
        try:
            return cls.from_yaml_string(
                raw_bytes.decode("utf-8"),
                source_path=resolved,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            )
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            log.error("YAML de cameras invalido em %s: %s", resolved, e)
            return cls(
                cameras=[],
                source_path=resolved,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            )

    @classmethod
    def from_yaml_string(
        cls,
        text: str,
        *,
        source_path: Path | None = None,
        content_hash: str | None = None,
    ) -> CamerasCatalog:
        h = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        data = yaml.safe_load(text)
        cameras = cls._parse_data(data)
        if source_path:
            log.info("Catalogo de cameras: %s (%s camera(s))", source_path, len(cameras))
        return cls(cameras=cameras, source_path=source_path, content_hash=h)

    @staticmethod
    def _parse_data(data: Any) -> list[CameraConfig]:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("O YAML deve ser um mapa na raiz (cameras:).")

        cameras: list[CameraConfig] = []
        for row in data.get("cameras") or []:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            cameras.append(
                CameraConfig(
                    id=cid,
                    name=str(row.get("name") or cid).strip(),
                    description=str(row.get("description") or "").strip(),
                )
            )
        return cameras

    @staticmethod
    def validate_structure(data: Any) -> list[str]:
        errors: list[str] = []

        if data is None:
            return ["Documento vazio. Defina a secao 'cameras:'."]
        if not isinstance(data, dict):
            return ["A raiz do arquivo deve ser um mapa YAML (chave: valor)."]

        for key in sorted(set(data.keys()) - ALLOWED_ROOT_KEYS):
            errors.append(f"Chave invalida na raiz: '{key}' (permitido: cameras).")

        if "cameras" not in data:
            errors.append("Defina a secao 'cameras:'.")
        elif not isinstance(data["cameras"], list):
            errors.append("'cameras' deve ser uma lista.")
        elif not data["cameras"]:
            errors.append("'cameras' nao pode estar vazia.")
        else:
            seen_ids: set[str] = set()
            for i, row in enumerate(data["cameras"]):
                path = f"cameras[{i}]"
                if not isinstance(row, dict):
                    errors.append(f"{path}: cada camera deve ser um mapa (id, name, description).")
                    continue
                cid = row.get("id")
                if not isinstance(cid, str) or not cid.strip():
                    errors.append(f"{path}: 'id' obrigatorio (igual ao nome no Frigate).")
                    continue
                cid = cid.strip()
                if not CAMERA_ID_RE.match(cid):
                    errors.append(
                        f"{path}: id invalido '{cid}' (use letras, numeros e _, ex.: Porta_Vidro)."
                    )
                elif cid in seen_ids:
                    errors.append(f"{path}: id duplicado '{cid}'.")
                else:
                    seen_ids.add(cid)
                name = row.get("name")
                if name is not None and not isinstance(name, str):
                    errors.append(f"{path}: 'name' deve ser texto.")
                desc = row.get("description")
                if desc is not None and not isinstance(desc, str):
                    errors.append(f"{path}: 'description' deve ser texto.")
                extra = set(row.keys()) - {"id", "name", "description"}
                for k in sorted(extra):
                    errors.append(f"{path}: chave desconhecida '{k}' (use id, name, description).")

        return errors

    def camera_map(self) -> dict[str, CameraConfig]:
        return {c.id: c for c in self.cameras}

    def resolve_camera_id(self, camera_id: str | None) -> str | None:
        """Resolve id da API Frigate por id ou nome amigavel."""
        if not camera_id or not str(camera_id).strip():
            return None
        needle = str(camera_id).strip().lower()
        for cam in self.cameras:
            if cam.id.lower() == needle:
                return cam.id
        for cam in self.cameras:
            if cam.name.lower() == needle:
                return cam.id
        return None

    def build_catalog_context(self) -> str:
        lines: list[str] = [
            "CAMERAS FRIGATE (use get_camera_snapshot para enviar foto ao usuario):",
            "",
        ]
        for cam in self.cameras:
            lines.append(f"  - id: {cam.id}")
            if cam.name:
                lines.append(f"    nome: {cam.name}")
            if cam.description:
                lines.append(f"    descricao: {cam.description}")
        if not self.cameras:
            lines.append("(Nenhuma camera configurada em shakira_cameras.yaml.)")
        return "\n".join(lines)


def resolve_cameras_path(configured: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if configured and str(configured).strip():
        candidates.append(Path(str(configured).strip()))
    env = os.environ.get("SHAKIRA_CAMERAS_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    for p in FALLBACK_CAMERAS_PATHS:
        candidates.append(Path(p))
    candidates.append(Path(DEFAULT_CAMERAS_PATH))

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            if configured and key != str(Path(str(configured).strip())):
                log.info("Usando cameras em %s (caminho configurado nao encontrado)", path)
            return path

    return (
        Path(str(configured).strip())
        if configured and str(configured).strip()
        else Path(DEFAULT_CAMERAS_PATH)
    )
