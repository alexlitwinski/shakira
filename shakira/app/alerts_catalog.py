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

ALLOWED_ROOT_KEYS = frozenset({"alerts", "alarm_dispatch"})
ALERT_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$", re.IGNORECASE)
ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]+\.[a-z0-9_]+$", re.IGNORECASE)
INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])?$", re.IGNORECASE)

MIN_CHECK_INTERVAL_SECONDS = 60
MAX_CHECK_INTERVAL_SECONDS = 86_400
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_COOLDOWN_SECONDS = 3600
LIVE_INTERVAL_TOKEN = "live"


def is_live_interval(value: Any) -> bool:
    """True se check_interval for o modo live (WebSocket HA)."""
    if isinstance(value, str) and value.strip().lower() == LIVE_INTERVAL_TOKEN:
        return True
    return False


def live_alerts(catalog: AlertsCatalog) -> list[AlertConfig]:
    return [a for a in catalog.alerts if a.enabled and a.live]


def polling_alerts(catalog: AlertsCatalog) -> list[AlertConfig]:
    return [a for a in catalog.alerts if a.enabled and not a.live]


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


def _parse_watch_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]


@dataclass
class AlertNotifyConfig:
    phones: list[str] = field(default_factory=list)


DEFAULT_ALARM_COOLDOWN_SECONDS = 300
DEFAULT_ALARM_DEBOUNCE_SECONDS = 2.0


@dataclass
class AlarmDispatchConfig:
    enabled: bool = False
    cooldown_seconds: int = DEFAULT_ALARM_COOLDOWN_SECONDS
    debounce_seconds: float = DEFAULT_ALARM_DEBOUNCE_SECONDS
    describe_cameras: bool = True
    notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)


@dataclass
class AlertConfig:
    id: str
    entity_id: str
    when_state: str
    message: str
    enabled: bool = True
    live: bool = False
    check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)
    recovery_when_state: str = ""
    recovery_context: str = ""
    recovery_label: str = ""
    camera_group: str = ""
    describe_cameras: bool = False
    describe_cameras_watch: list[str] = field(default_factory=list)


@dataclass
class AlertsCatalog:
    alerts: list[AlertConfig] = field(default_factory=list)
    alarm_dispatch: AlarmDispatchConfig = field(default_factory=AlarmDispatchConfig)
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
        alarm_dispatch = cls._parse_alarm_dispatch(data)
        enabled = sum(1 for a in alerts if a.enabled)
        if source_path:
            log.info(
                "Alertas carregados: %s (%s regra(s), %s ativa(s), alarm_dispatch=%s)",
                source_path,
                len(alerts),
                enabled,
                alarm_dispatch.enabled,
            )
        return cls(
            alerts=alerts,
            alarm_dispatch=alarm_dispatch,
            source_path=source_path,
            content_hash=h,
        )

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

        live = is_live_interval(interval_raw)
        if live:
            interval = DEFAULT_CHECK_INTERVAL_SECONDS
        else:
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

        phones = cls._parse_notify_phones(row.get("notify"))

        return AlertConfig(
            id=aid,
            entity_id=entity_id,
            when_state=when_state,
            message=message,
            enabled=bool(row.get("enabled", True)),
            live=live,
            check_interval_seconds=interval,
            cooldown_seconds=cooldown,
            notify=AlertNotifyConfig(phones=phones),
            recovery_when_state=str(row.get("recovery_when_state") or "").strip(),
            recovery_context=str(row.get("recovery_context") or "").strip(),
            recovery_label=str(row.get("recovery_label") or "").strip(),
            camera_group=str(row.get("camera_group") or "").strip(),
            describe_cameras=bool(row.get("describe_cameras", False)),
            describe_cameras_watch=_parse_watch_names(row.get("describe_cameras_watch")),
        )

    @staticmethod
    def _parse_notify_phones(notify: Any) -> list[str]:
        phones: list[str] = []
        if isinstance(notify, dict):
            raw_phones = notify.get("phones")
            if isinstance(raw_phones, list):
                for p in raw_phones:
                    if isinstance(p, (str, int)):
                        s = str(p).strip()
                        if s:
                            phones.append(s)
        return phones

    @classmethod
    def _parse_alarm_dispatch(cls, data: Any) -> AlarmDispatchConfig:
        if not isinstance(data, dict):
            return AlarmDispatchConfig()
        block = data.get("alarm_dispatch")
        if not isinstance(block, dict):
            return AlarmDispatchConfig()

        cooldown_raw = block.get("cooldown_seconds")
        if cooldown_raw is None:
            cooldown_raw = block.get("cooldown")
        cooldown = parse_interval_seconds(cooldown_raw)
        if cooldown is None:
            cooldown = DEFAULT_ALARM_COOLDOWN_SECONDS
        cooldown = max(MIN_CHECK_INTERVAL_SECONDS, min(cooldown, MAX_CHECK_INTERVAL_SECONDS * 7))

        debounce_raw = block.get("debounce_seconds")
        debounce = DEFAULT_ALARM_DEBOUNCE_SECONDS
        if isinstance(debounce_raw, (int, float)) and not isinstance(debounce_raw, bool):
            debounce = max(0.5, min(float(debounce_raw), 30.0))

        return AlarmDispatchConfig(
            enabled=bool(block.get("enabled", False)),
            cooldown_seconds=cooldown,
            debounce_seconds=debounce,
            describe_cameras=bool(block.get("describe_cameras", True)),
            notify=AlertNotifyConfig(phones=cls._parse_notify_phones(block.get("notify"))),
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
            errors.append(f"Chave invalida na raiz: '{key}' (permitido: alerts, alarm_dispatch).")

        alarm_block = data.get("alarm_dispatch")
        if alarm_block is not None:
            if not isinstance(alarm_block, dict):
                errors.append("'alarm_dispatch' deve ser um mapa.")
            else:
                alarm_allowed = {
                    "enabled",
                    "cooldown",
                    "cooldown_seconds",
                    "debounce_seconds",
                    "describe_cameras",
                    "notify",
                }
                for k in sorted(set(alarm_block.keys()) - alarm_allowed):
                    errors.append(f"alarm_dispatch: chave desconhecida '{k}'.")
                if "enabled" in alarm_block and not isinstance(alarm_block["enabled"], bool):
                    errors.append("alarm_dispatch.enabled deve ser true ou false.")
                describe = alarm_block.get("describe_cameras")
                if describe is not None and not isinstance(describe, bool):
                    errors.append("alarm_dispatch.describe_cameras deve ser true ou false.")
                debounce = alarm_block.get("debounce_seconds")
                if debounce is not None and not isinstance(debounce, (int, float)):
                    errors.append("alarm_dispatch.debounce_seconds deve ser numero.")
                for cooldown_key in ("cooldown", "cooldown_seconds"):
                    if cooldown_key in alarm_block:
                        parsed = parse_interval_seconds(alarm_block[cooldown_key])
                        if parsed is None:
                            errors.append(f"alarm_dispatch.{cooldown_key} invalido.")
                notify = alarm_block.get("notify")
                if notify is not None:
                    if not isinstance(notify, dict):
                        errors.append("alarm_dispatch.notify deve ser um mapa.")
                    elif notify.get("phones") is not None and not isinstance(
                        notify.get("phones"), list
                    ):
                        errors.append("alarm_dispatch.notify.phones deve ser uma lista.")

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
            "camera_group",
            "describe_cameras",
            "describe_cameras_watch",
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

            camera_group = row.get("camera_group")
            if camera_group is not None and not isinstance(camera_group, str):
                errors.append(f"{path}: 'camera_group' deve ser texto.")

            describe_cameras = row.get("describe_cameras")
            if describe_cameras is not None and not isinstance(describe_cameras, bool):
                errors.append(f"{path}: 'describe_cameras' deve ser true ou false.")
            if describe_cameras and not (
                isinstance(row.get("camera_group"), str) and str(row.get("camera_group")).strip()
            ):
                errors.append(
                    f"{path}: 'describe_cameras' requer 'camera_group' definido."
                )

            raw_watch = row.get("describe_cameras_watch")
            if raw_watch is not None:
                if not isinstance(raw_watch, list):
                    errors.append(f"{path}: 'describe_cameras_watch' deve ser uma lista.")
                else:
                    for j, item in enumerate(raw_watch):
                        if not isinstance(item, str) or not item.strip():
                            errors.append(
                                f"{path}.describe_cameras_watch[{j}]: use nomes de camera em texto."
                            )

            if "enabled" in row and not isinstance(row["enabled"], bool):
                errors.append(f"{path}: 'enabled' deve ser true ou false.")

            for interval_key in ("check_interval", "check_interval_seconds"):
                if interval_key not in row:
                    continue
                raw_val = row[interval_key]
                if is_live_interval(raw_val):
                    if interval_key == "check_interval_seconds":
                        errors.append(
                            f"{path}: use check_interval: live (nao check_interval_seconds)."
                        )
                    continue
                parsed = parse_interval_seconds(raw_val)
                if parsed is None:
                    errors.append(
                        f"{path}: '{interval_key}' invalido (use live, segundos ou ex.: 5m, 1h)."
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

    def enabled_live_alerts(self) -> list[AlertConfig]:
        return live_alerts(self)

    def enabled_polling_alerts(self) -> list[AlertConfig]:
        return polling_alerts(self)
