"""Catalogo de alertas periodicos (YAML em /config)."""

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

DEFAULT_ALERTS_PATH = "/config/shakira_alerts.yaml"
FALLBACK_ALERTS_PATHS = (
    "/homeassistant/shakira_alerts.yaml",
    "/config/shakira_alerts.yaml",
)

ALLOWED_ROOT_KEYS = frozenset({"alerts"})
ALERT_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$", re.IGNORECASE)
ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]+\.[a-z0-9_]+$", re.IGNORECASE)
INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])?$", re.IGNORECASE)

MIN_CHECK_INTERVAL_SECONDS = 60
MAX_CHECK_INTERVAL_SECONDS = 86_400
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_COOLDOWN_SECONDS = 3600


class AlertsCatalogValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def resolve_alerts_path(configured: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if configured and str(configured).strip():
        candidates.append(Path(str(configured).strip()))
    env = os.environ.get("SHAKIRA_ALERTS_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    for p in FALLBACK_ALERTS_PATHS:
        candidates.append(Path(p))
    candidates.append(Path(DEFAULT_ALERTS_PATH))

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            if configured and key != str(Path(str(configured).strip())):
                log.info("Usando alertas em %s (caminho configurado nao encontrado)", path)
            return path

    return (
        Path(str(configured).strip())
        if configured and str(configured).strip()
        else Path(DEFAULT_ALERTS_PATH)
    )


def parse_interval_seconds(value: Any, *, field_path: str = "check_interval") -> int | None:
    """Converte segundos (int) ou string '5m', '1h', '90s' em segundos."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        m = INTERVAL_RE.match(raw.replace(" ", ""))
        if not m:
            return None
        amount = int(m.group(1))
        unit = (m.group(2) or "s").lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86_400}.get(unit)
        if mult is None:
            return None
        return amount * mult
    return None


def clamp_interval(seconds: int) -> int:
    return max(MIN_CHECK_INTERVAL_SECONDS, min(seconds, MAX_CHECK_INTERVAL_SECONDS))


@dataclass
class AlertNotifyConfig:
    phones: list[str] = field(default_factory=list)


@dataclass
class AlertConfig:
    id: str
    entity_id: str
    when_state: str
    message: str
    enabled: bool = True
    check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)
    recovery_when_state: str = ""
    recovery_context: str = ""
    recovery_label: str = ""


@dataclass
class AlertsCatalog:
    alerts: list[AlertConfig] = field(default_factory=list)
    source_path: Path | None = None
    content_hash: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> AlertsCatalog:
        resolved = resolve_alerts_path(path)
        if not resolved.is_file():
            log.info("Arquivo de alertas nao encontrado: %s", resolved)
            return cls(alerts=[], source_path=resolved, content_hash="")

        raw_bytes = resolved.read_bytes()
        try:
            return cls.from_yaml_string(
                raw_bytes.decode("utf-8"),
                source_path=resolved,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
            )
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            log.error("YAML de alertas invalido em %s: %s", resolved, e)
            return cls(
                alerts=[],
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
    ) -> AlertsCatalog:
        h = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        data = yaml.safe_load(text)
        alerts = cls._parse_data(data)
        enabled = sum(1 for a in alerts if a.enabled)
        if source_path:
            log.info(
                "Alertas carregados: %s (%s regra(s), %s ativa(s))",
                source_path,
                len(alerts),
                enabled,
            )
        return cls(alerts=alerts, source_path=source_path, content_hash=h)

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> AlertConfig | None:
        aid = str(row.get("id") or "").strip()
        if not aid:
            return None
        entity_id = str(row.get("entity_id") or "").strip()
        when_state = str(row.get("when_state") or "").strip()
        message = str(row.get("message") or "").strip()
        if not entity_id or not when_state or not message:
            return None

        interval_raw = row.get("check_interval_seconds")
        if interval_raw is None:
            interval_raw = row.get("check_interval")
        interval = parse_interval_seconds(interval_raw)
        if interval is None:
            interval = DEFAULT_CHECK_INTERVAL_SECONDS
        interval = clamp_interval(interval)

        cooldown_raw = row.get("cooldown_seconds")
        if cooldown_raw is None:
            cooldown_raw = row.get("cooldown")
        cooldown = parse_interval_seconds(cooldown_raw)
        if cooldown is None:
            cooldown = DEFAULT_COOLDOWN_SECONDS
        cooldown = max(MIN_CHECK_INTERVAL_SECONDS, min(cooldown, MAX_CHECK_INTERVAL_SECONDS * 7))

        phones: list[str] = []
        notify = row.get("notify")
        if isinstance(notify, dict):
            raw_phones = notify.get("phones")
            if isinstance(raw_phones, list):
                for p in raw_phones:
                    if isinstance(p, (str, int)):
                        s = str(p).strip()
                        if s:
                            phones.append(s)

        return AlertConfig(
            id=aid,
            entity_id=entity_id,
            when_state=when_state,
            message=message,
            enabled=bool(row.get("enabled", True)),
            check_interval_seconds=interval,
            cooldown_seconds=cooldown,
            notify=AlertNotifyConfig(phones=phones),
            recovery_when_state=str(row.get("recovery_when_state") or "").strip(),
            recovery_context=str(row.get("recovery_context") or "").strip(),
            recovery_label=str(row.get("recovery_label") or "").strip(),
        )

    @classmethod
    def _parse_data(cls, data: Any) -> list[AlertConfig]:
        if data is None:
            return []
        if not isinstance(data, dict):
            raise ValueError("O YAML deve ser um mapa na raiz (alerts:).")

        alerts: list[AlertConfig] = []
        for row in data.get("alerts") or []:
            if not isinstance(row, dict):
                continue
            parsed = cls._parse_row(row)
            if parsed:
                alerts.append(parsed)
        return alerts

    @staticmethod
    def validate_structure(data: Any) -> list[str]:
        errors: list[str] = []

        if data is None:
            return ["Documento vazio. Defina 'alerts:' com ao menos uma regra."]
        if not isinstance(data, dict):
            return ["A raiz do arquivo deve ser um mapa YAML (chave: valor)."]

        for key in sorted(set(data.keys()) - ALLOWED_ROOT_KEYS):
            errors.append(f"Chave invalida na raiz: '{key}' (permitido: alerts).")

        if "alerts" not in data:
            errors.append("Defina a secao 'alerts:'.")
            return errors

        if not isinstance(data["alerts"], list):
            errors.append("'alerts' deve ser uma lista.")
            return errors

        seen_ids: set[str] = set()
        allowed_keys = {
            "id",
            "enabled",
            "entity_id",
            "when_state",
            "message",
            "check_interval",
            "check_interval_seconds",
            "cooldown",
            "cooldown_seconds",
            "notify",
            "recovery_when_state",
            "recovery_context",
            "recovery_label",
        }

        for i, row in enumerate(data["alerts"]):
            path = f"alerts[{i}]"
            if not isinstance(row, dict):
                errors.append(f"{path}: cada alerta deve ser um mapa.")
                continue

            for k in sorted(set(row.keys()) - allowed_keys):
                errors.append(f"{path}: chave desconhecida '{k}'.")

            aid = row.get("id")
            if not isinstance(aid, str) or not aid.strip():
                errors.append(f"{path}: 'id' obrigatorio.")
            else:
                aid = aid.strip()
                if not ALERT_ID_RE.match(aid):
                    errors.append(f"{path}: id invalido '{aid}' (use letras, numeros e _).")
                elif aid in seen_ids:
                    errors.append(f"{path}: id duplicado '{aid}'.")
                else:
                    seen_ids.add(aid)

            eid = row.get("entity_id")
            if not isinstance(eid, str) or not eid.strip():
                errors.append(f"{path}: 'entity_id' obrigatorio.")
            elif not ENTITY_ID_RE.match(eid.strip()):
                errors.append(f"{path}: entity_id invalido '{eid}'.")

            when_state = row.get("when_state")
            if not isinstance(when_state, str) or not when_state.strip():
                errors.append(
                    f"{path}: 'when_state' obrigatorio (ex.: on, off, unavailable, >=85, >35)."
                )

            message = row.get("message")
            if not isinstance(message, str) or not message.strip():
                errors.append(f"{path}: 'message' obrigatorio.")

            recovery_when = row.get("recovery_when_state")
            recovery_ctx = row.get("recovery_context")
            has_recovery_when = isinstance(recovery_when, str) and recovery_when.strip()
            has_recovery_ctx = isinstance(recovery_ctx, str) and recovery_ctx.strip()
            if has_recovery_when and not has_recovery_ctx:
                errors.append(
                    f"{path}: 'recovery_context' obrigatorio quando 'recovery_when_state' esta definido."
                )
            if has_recovery_ctx and not has_recovery_when:
                errors.append(
                    f"{path}: 'recovery_when_state' obrigatorio quando 'recovery_context' esta definido."
                )

            recovery_label = row.get("recovery_label")
            if recovery_label is not None and not isinstance(recovery_label, str):
                errors.append(f"{path}: 'recovery_label' deve ser texto.")

            if "enabled" in row and not isinstance(row["enabled"], bool):
                errors.append(f"{path}: 'enabled' deve ser true ou false.")

            for interval_key in ("check_interval", "check_interval_seconds"):
                if interval_key not in row:
                    continue
                parsed = parse_interval_seconds(row[interval_key])
                if parsed is None:
                    errors.append(
                        f"{path}: '{interval_key}' invalido (use segundos ou ex.: 5m, 1h)."
                    )
                elif parsed < MIN_CHECK_INTERVAL_SECONDS:
                    errors.append(
                        f"{path}: intervalo minimo e {MIN_CHECK_INTERVAL_SECONDS}s."
                    )

            for cooldown_key in ("cooldown", "cooldown_seconds"):
                if cooldown_key not in row:
                    continue
                parsed = parse_interval_seconds(row[cooldown_key])
                if parsed is None:
                    errors.append(f"{path}: '{cooldown_key}' invalido.")

            notify = row.get("notify")
            if notify is not None:
                if not isinstance(notify, dict):
                    errors.append(f"{path}: 'notify' deve ser um mapa.")
                else:
                    phones = notify.get("phones")
                    if phones is not None and not isinstance(phones, list):
                        errors.append(f"{path}.notify.phones deve ser uma lista.")

        return errors

    def enabled_alerts(self) -> list[AlertConfig]:
        return [a for a in self.alerts if a.enabled]
