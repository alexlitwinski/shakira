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

ALLOWED_ROOT_KEYS = frozenset(
    {
        "alerts",
        "alarm_dispatch",
        "rain_dispatch",
        "interfone_dispatch",
        "presence_simulator",
        "default_notify",
        "double_take_dispatch",
    }
)
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
DEFAULT_RAIN_COOLDOWN_SECONDS = 900
DEFAULT_HEAVY_RAIN_MM = 4.0

DEFAULT_RAIN_ENTITY = "binary_sensor.sensor_chuva_e_lux_rain"
DEFAULT_RAIN_VOLUME_ENTITY = "sensor.volume_de_chuva_15m"
DEFAULT_PORTA_VIDRO_ENTITY = "cover.porta_de_vidro_gourmet"
DEFAULT_TOLDO_ENTITY = "cover.toldo_gourmet"

DEFAULT_INTERFONE_ENTITY = "input_boolean.interfone_tocando"
DEFAULT_INTERFONE_CAMERA_ID = "Porta_Vidro"
DEFAULT_PORTAO_SOCIAL_ENTITY = "sensor.amt_8000_zone_1"
DEFAULT_PORTAO_SERVICO_ENTITY = "sensor.amt_8000_zone_7"
DEFAULT_HALL_PERSON_ENTITY = "binary_sensor.hall_interno_person_occupancy"
DEFAULT_INTERFONE_ATTEND_SECONDS = 180
DEFAULT_INTERFONE_DEBOUNCE_SECONDS = 10.0


@dataclass
class PresenceSimulatorConfig:
    enabled: bool = False
    control_entity: str = "input_boolean.simular_luzes"
    min_on_minutes: float = 5.0
    max_on_minutes: float = 25.0
    min_off_minutes: float = 10.0
    max_off_minutes: float = 40.0


@dataclass
class InterfoneDispatchConfig:
    enabled: bool = False
    interfone_entity: str = DEFAULT_INTERFONE_ENTITY
    camera_id: str = DEFAULT_INTERFONE_CAMERA_ID
    attend_window_seconds: int = DEFAULT_INTERFONE_ATTEND_SECONDS
    debounce_seconds: float = DEFAULT_INTERFONE_DEBOUNCE_SECONDS
    portao_social_entity: str = DEFAULT_PORTAO_SOCIAL_ENTITY
    portao_servico_entity: str = DEFAULT_PORTAO_SERVICO_ENTITY
    hall_person_entity: str = DEFAULT_HALL_PERSON_ENTITY
    data_path: str = ""


@dataclass
class RainDispatchConfig:
    enabled: bool = False
    cooldown_seconds: int = DEFAULT_RAIN_COOLDOWN_SECONDS
    heavy_rain_mm: float = DEFAULT_HEAVY_RAIN_MM
    format_message_with_gemini: bool = True
    notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)
    rain_entity: str = DEFAULT_RAIN_ENTITY
    volume_entity: str = DEFAULT_RAIN_VOLUME_ENTITY
    porta_vidro_entity: str = DEFAULT_PORTA_VIDRO_ENTITY
    toldo_entity: str = DEFAULT_TOLDO_ENTITY


@dataclass
class AlarmDispatchConfig:
    enabled: bool = False
    cooldown_seconds: int = DEFAULT_ALARM_COOLDOWN_SECONDS
    debounce_seconds: float = DEFAULT_ALARM_DEBOUNCE_SECONDS
    describe_cameras: bool = True
    notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)


DEFAULT_DOUBLE_TAKE_MIN_CONFIDENCE = 85

@dataclass
class DoubleTakeDispatchConfig:
    enabled: bool = False
    min_confidence: int = DEFAULT_DOUBLE_TAKE_MIN_CONFIDENCE


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
    rain_dispatch: RainDispatchConfig = field(default_factory=RainDispatchConfig)
    interfone_dispatch: InterfoneDispatchConfig = field(
        default_factory=InterfoneDispatchConfig
    )
    presence_simulator: PresenceSimulatorConfig = field(
        default_factory=PresenceSimulatorConfig
    )
    double_take_dispatch: DoubleTakeDispatchConfig = field(
        default_factory=DoubleTakeDispatchConfig
    )
    default_notify: AlertNotifyConfig = field(default_factory=AlertNotifyConfig)
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
        rain_dispatch = cls._parse_rain_dispatch(data)
        interfone_dispatch = cls._parse_interfone_dispatch(data)
        presence_simulator = cls._parse_presence_simulator(data)
        double_take_dispatch = cls._parse_double_take_dispatch(data)
        default_notify = cls._parse_default_notify(data)
        enabled = sum(1 for a in alerts if a.enabled)
        if source_path:
            log.info(
                "Alertas carregados: %s (%s regra(s), %s ativa(s), alarm_dispatch=%s, "
                "rain_dispatch=%s, interfone_dispatch=%s, presence_simulator=%s, double_take_dispatch=%s, default_notify=%s telefone(s))",
                source_path,
                len(alerts),
                enabled,
                alarm_dispatch.enabled,
                rain_dispatch.enabled,
                interfone_dispatch.enabled,
                presence_simulator.enabled,
                double_take_dispatch.enabled,
                len(default_notify.phones),
            )
        return cls(
            alerts=alerts,
            alarm_dispatch=alarm_dispatch,
            rain_dispatch=rain_dispatch,
            interfone_dispatch=interfone_dispatch,
            presence_simulator=presence_simulator,
            double_take_dispatch=double_take_dispatch,
            default_notify=default_notify,
            source_path=source_path,
            content_hash=h,
        )

    @classmethod
    def _parse_default_notify(cls, data: Any) -> AlertNotifyConfig:
        if not isinstance(data, dict):
            return AlertNotifyConfig()
        return AlertNotifyConfig(phones=cls._parse_notify_phones(data.get("default_notify")))

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

        phones = AlertsCatalog._parse_notify_phones(row.get("notify"))

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
    def _parse_rain_dispatch(cls, data: Any) -> RainDispatchConfig:
        if not isinstance(data, dict):
            return RainDispatchConfig()
        block = data.get("rain_dispatch")
        if not isinstance(block, dict):
            return RainDispatchConfig()

        cooldown_raw = block.get("cooldown_seconds")
        if cooldown_raw is None:
            cooldown_raw = block.get("cooldown")
        cooldown = parse_interval_seconds(cooldown_raw)
        if cooldown is None:
            cooldown = DEFAULT_RAIN_COOLDOWN_SECONDS
        cooldown = max(MIN_CHECK_INTERVAL_SECONDS, min(cooldown, MAX_CHECK_INTERVAL_SECONDS * 7))

        heavy_raw = block.get("heavy_rain_mm")
        heavy = DEFAULT_HEAVY_RAIN_MM
        if isinstance(heavy_raw, (int, float)) and not isinstance(heavy_raw, bool):
            heavy = max(0.1, min(float(heavy_raw), 500.0))

        entities = block.get("entities")
        ent_map: dict[str, str] = {}
        if isinstance(entities, dict):
            for key in ("rain", "volume_15m", "porta_vidro", "toldo"):
                val = entities.get(key)
                if isinstance(val, str) and val.strip():
                    ent_map[key] = val.strip()

        return RainDispatchConfig(
            enabled=bool(block.get("enabled", False)),
            cooldown_seconds=cooldown,
            heavy_rain_mm=heavy,
            format_message_with_gemini=bool(block.get("format_message_with_gemini", True)),
            notify=AlertNotifyConfig(phones=cls._parse_notify_phones(block.get("notify"))),
            rain_entity=ent_map.get("rain", DEFAULT_RAIN_ENTITY),
            volume_entity=ent_map.get("volume_15m", DEFAULT_RAIN_VOLUME_ENTITY),
            porta_vidro_entity=ent_map.get("porta_vidro", DEFAULT_PORTA_VIDRO_ENTITY),
            toldo_entity=ent_map.get("toldo", DEFAULT_TOLDO_ENTITY),
        )

    @classmethod
    def _parse_interfone_dispatch(cls, data: Any) -> InterfoneDispatchConfig:
        if not isinstance(data, dict):
            return InterfoneDispatchConfig()
        block = data.get("interfone_dispatch")
        if not isinstance(block, dict):
            return InterfoneDispatchConfig()

        window_raw = block.get("attend_window_seconds")
        if window_raw is None:
            window_raw = block.get("attend_window")
        window_sec = DEFAULT_INTERFONE_ATTEND_SECONDS
        if isinstance(window_raw, (int, float)) and not isinstance(window_raw, bool):
            window_sec = int(window_raw)
        elif isinstance(window_raw, str):
            parsed = parse_interval_seconds(window_raw)
            if parsed is not None:
                window_sec = parsed
        window_sec = max(60, min(window_sec, 600))

        debounce_raw = block.get("debounce_seconds")
        debounce = DEFAULT_INTERFONE_DEBOUNCE_SECONDS
        if isinstance(debounce_raw, (int, float)) and not isinstance(debounce_raw, bool):
            debounce = max(2.0, min(float(debounce_raw), 120.0))

        entities = block.get("entities")
        ent_map: dict[str, str] = {}
        if isinstance(entities, dict):
            for key in (
                "interfone",
                "portao_social",
                "portao_servico",
                "hall_person",
            ):
                val = entities.get(key)
                if isinstance(val, str) and val.strip():
                    ent_map[key] = val.strip()

        camera_id = block.get("camera_id")
        if isinstance(camera_id, str) and camera_id.strip():
            cam_id = camera_id.strip()
        else:
            cam_id = DEFAULT_INTERFONE_CAMERA_ID

        data_path = ""
        raw_path = block.get("data_path")
        if isinstance(raw_path, str) and raw_path.strip():
            data_path = raw_path.strip()

        return InterfoneDispatchConfig(
            enabled=bool(block.get("enabled", False)),
            interfone_entity=ent_map.get("interfone", DEFAULT_INTERFONE_ENTITY),
            camera_id=cam_id,
            attend_window_seconds=window_sec,
            debounce_seconds=debounce,
            portao_social_entity=ent_map.get(
                "portao_social", DEFAULT_PORTAO_SOCIAL_ENTITY
            ),
            portao_servico_entity=ent_map.get(
                "portao_servico", DEFAULT_PORTAO_SERVICO_ENTITY
            ),
            hall_person_entity=ent_map.get("hall_person", DEFAULT_HALL_PERSON_ENTITY),
            data_path=data_path,
        )

    @classmethod
    def _parse_presence_simulator(cls, data: Any) -> PresenceSimulatorConfig:
        if not isinstance(data, dict):
            return PresenceSimulatorConfig()
        block = data.get("presence_simulator")
        if not isinstance(block, dict):
            return PresenceSimulatorConfig()

        def get_float(b: dict, key: str, default: float) -> float:
            val = b.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
            return default

        return PresenceSimulatorConfig(
            enabled=bool(block.get("enabled", False)),
            control_entity=str(block.get("control_entity", "input_boolean.simular_luzes")).strip(),
            min_on_minutes=get_float(block, "min_on_minutes", 5.0),
            max_on_minutes=get_float(block, "max_on_minutes", 25.0),
            min_off_minutes=get_float(block, "min_off_minutes", 10.0),
            max_off_minutes=get_float(block, "max_off_minutes", 40.0),
        )

    @classmethod
    def _parse_double_take_dispatch(cls, data: Any) -> DoubleTakeDispatchConfig:
        if not isinstance(data, dict):
            return DoubleTakeDispatchConfig()
        block = data.get("double_take_dispatch")
        if not isinstance(block, dict):
            return DoubleTakeDispatchConfig()

        min_conf_raw = block.get("min_confidence")
        min_conf = DEFAULT_DOUBLE_TAKE_MIN_CONFIDENCE
        if isinstance(min_conf_raw, (int, float)) and not isinstance(min_conf_raw, bool):
            min_conf = max(10, min(int(min_conf_raw), 100))

        return DoubleTakeDispatchConfig(
            enabled=bool(block.get("enabled", False)),
            min_confidence=min_conf,
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
            errors.append(
                f"Chave invalida na raiz: '{key}' "
                "(permitido: alerts, alarm_dispatch, rain_dispatch, interfone_dispatch, "
                "default_notify)."
            )

        default_notify = data.get("default_notify")
        if default_notify is not None:
            if not isinstance(default_notify, dict):
                errors.append("default_notify deve ser um mapa.")
            elif default_notify.get("phones") is not None and not isinstance(
                default_notify.get("phones"), list
            ):
                errors.append("default_notify.phones deve ser uma lista.")

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

        rain_block = data.get("rain_dispatch")
        if rain_block is not None:
            if not isinstance(rain_block, dict):
                errors.append("'rain_dispatch' deve ser um mapa.")
            else:
                rain_allowed = {
                    "enabled",
                    "cooldown",
                    "cooldown_seconds",
                    "heavy_rain_mm",
                    "format_message_with_gemini",
                    "notify",
                    "entities",
                }
                for k in sorted(set(rain_block.keys()) - rain_allowed):
                    errors.append(f"rain_dispatch: chave desconhecida '{k}'.")
                if "enabled" in rain_block and not isinstance(rain_block["enabled"], bool):
                    errors.append("rain_dispatch.enabled deve ser true ou false.")
                heavy = rain_block.get("heavy_rain_mm")
                if heavy is not None and not isinstance(heavy, (int, float)):
                    errors.append("rain_dispatch.heavy_rain_mm deve ser numero.")
                fmt_gemini = rain_block.get("format_message_with_gemini")
                if fmt_gemini is not None and not isinstance(fmt_gemini, bool):
                    errors.append(
                        "rain_dispatch.format_message_with_gemini deve ser true ou false."
                    )
                for cooldown_key in ("cooldown", "cooldown_seconds"):
                    if cooldown_key in rain_block:
                        parsed = parse_interval_seconds(rain_block[cooldown_key])
                        if parsed is None:
                            errors.append(f"rain_dispatch.{cooldown_key} invalido.")
                notify = rain_block.get("notify")
                if notify is not None:
                    if not isinstance(notify, dict):
                        errors.append("rain_dispatch.notify deve ser um mapa.")
                    elif notify.get("phones") is not None and not isinstance(
                        notify.get("phones"), list
                    ):
                        errors.append("rain_dispatch.notify.phones deve ser uma lista.")
                entities = rain_block.get("entities")
                if entities is not None:
                    if not isinstance(entities, dict):
                        errors.append("rain_dispatch.entities deve ser um mapa.")
                    else:
                        for ek in sorted(set(entities.keys()) - {"rain", "volume_15m", "porta_vidro", "toldo"}):
                            errors.append(f"rain_dispatch.entities: chave desconhecida '{ek}'.")
                        for ek, ev in entities.items():
                            if not isinstance(ev, str) or not ev.strip():
                                errors.append(f"rain_dispatch.entities.{ek} deve ser entity_id texto.")
                            elif not ENTITY_ID_RE.match(ev.strip()):
                                errors.append(f"rain_dispatch.entities.{ek}: entity_id invalido.")

        interfone_block = data.get("interfone_dispatch")
        if interfone_block is not None:
            if not isinstance(interfone_block, dict):
                errors.append("'interfone_dispatch' deve ser um mapa.")
            else:
                if_allowed = {
                    "enabled",
                    "camera_id",
                    "attend_window",
                    "attend_window_seconds",
                    "debounce_seconds",
                    "data_path",
                    "entities",
                }
                for k in sorted(set(interfone_block.keys()) - if_allowed):
                    errors.append(f"interfone_dispatch: chave desconhecida '{k}'.")
                if "enabled" in interfone_block and not isinstance(
                    interfone_block["enabled"], bool
                ):
                    errors.append("interfone_dispatch.enabled deve ser true ou false.")
                for window_key in ("attend_window", "attend_window_seconds"):
                    if window_key in interfone_block:
                        parsed = parse_interval_seconds(interfone_block[window_key])
                        if parsed is None and not isinstance(
                            interfone_block[window_key], (int, float)
                        ):
                            errors.append(f"interfone_dispatch.{window_key} invalido.")
                debounce = interfone_block.get("debounce_seconds")
                if debounce is not None and not isinstance(debounce, (int, float)):
                    errors.append("interfone_dispatch.debounce_seconds deve ser numero.")
                cam = interfone_block.get("camera_id")
                if cam is not None and (not isinstance(cam, str) or not cam.strip()):
                    errors.append("interfone_dispatch.camera_id deve ser texto.")
                entities = interfone_block.get("entities")
                if entities is not None:
                    if not isinstance(entities, dict):
                        errors.append("interfone_dispatch.entities deve ser um mapa.")
                    else:
                        allowed_ent = {
                            "interfone",
                            "portao_social",
                            "portao_servico",
                            "hall_person",
                        }
                        for ek in sorted(set(entities.keys()) - allowed_ent):
                            errors.append(
                                f"interfone_dispatch.entities: chave desconhecida '{ek}'."
                            )
                        for ek, ev in entities.items():
                            if not isinstance(ev, str) or not ev.strip():
                                errors.append(
                                    f"interfone_dispatch.entities.{ek} deve ser entity_id texto."
                                )
                            elif not ENTITY_ID_RE.match(ev.strip()):
                                errors.append(
                                    f"interfone_dispatch.entities.{ek}: entity_id invalido."
                                )

        ps_block = data.get("presence_simulator")
        if ps_block is not None:
            if not isinstance(ps_block, dict):
                errors.append("'presence_simulator' deve ser um mapa.")
            else:
                ps_allowed = {
                    "enabled",
                    "control_entity",
                    "min_on_minutes",
                    "max_on_minutes",
                    "min_off_minutes",
                    "max_off_minutes",
                }
                for k in sorted(set(ps_block.keys()) - ps_allowed):
                    errors.append(f"presence_simulator: chave desconhecida '{k}'.")
                if "enabled" in ps_block and not isinstance(ps_block["enabled"], bool):
                     errors.append("presence_simulator.enabled deve ser true ou false.")
                if "control_entity" in ps_block:
                     ce = ps_block["control_entity"]
                     if not isinstance(ce, str) or not ce.strip() or not ENTITY_ID_RE.match(ce.strip()):
                         errors.append("presence_simulator.control_entity deve ser um entity_id valido.")
                for k in ("min_on_minutes", "max_on_minutes", "min_off_minutes", "max_off_minutes"):
                     if k in ps_block:
                         val = ps_block[k]
                         if not isinstance(val, (int, float)) or isinstance(val, bool):
                             errors.append(f"presence_simulator.{k} deve ser um numero.")

        dt_block = data.get("double_take_dispatch")
        if dt_block is not None:
            if not isinstance(dt_block, dict):
                errors.append("'double_take_dispatch' deve ser um mapa.")
            else:
                dt_allowed = {"enabled", "min_confidence"}
                for k in sorted(set(dt_block.keys()) - dt_allowed):
                    errors.append(f"double_take_dispatch: chave desconhecida '{k}'.")
                if "enabled" in dt_block and not isinstance(dt_block["enabled"], bool):
                    errors.append("double_take_dispatch.enabled deve ser true ou false.")
                if "min_confidence" in dt_block:
                    val = dt_block["min_confidence"]
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        errors.append("double_take_dispatch.min_confidence deve ser um numero.")

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
