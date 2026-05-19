"""Carrega configuracao do Supervisor e options.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.logging_config import normalize_log_level


def load_addon_options() -> dict:
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _opts_str(opts: dict, key: str, env_fallback: str) -> str:
    v = opts.get(key)
    if isinstance(v, str):
        raw = v.strip()
        if raw:
            return raw
    return os.environ.get(env_fallback, "").strip()


def _opts_int(opts: dict, key: str, default: int) -> int:
    v = opts.get(key)
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    env = os.environ.get(key.upper(), "")
    if env.isdigit():
        return int(env)
    return default


def _opts_log_level(opts: dict) -> str:
    v = opts.get("log_level")
    if isinstance(v, str) and v.strip():
        return normalize_log_level(v)
    env = os.environ.get("SHAKIRA_LOG_LEVEL", "").strip()
    if env:
        return normalize_log_level(env)
    return "info"


@dataclass
class AppSettings:
    """Configuracao em runtime."""

    supervisor_token: str
    ha_url: str
    evolution_base_url: str
    evolution_api_key: str
    gemini_api_key: str
    evolution_instance: str
    devices_config_path: str
    gemini_cache_ttl_hours: int
    photoprism_url: str
    photoprism_token: str
    photoprism_max_photos: int
    photoprism_api_prefix: str
    frigate_url: str
    frigate_cameras_config_path: str
    alerts_config_path: str
    shakira_api_token: str
    log_level: str

    @classmethod
    def load(cls) -> AppSettings:
        opts = load_addon_options()
        opt_token = opts.get("homeassistant_long_lived_token")
        opt_llt = ""
        if isinstance(opt_token, str):
            opt_llt = opt_token.strip()

        token = (
            os.environ.get("SUPERVISOR_TOKEN")
            or os.environ.get("HASSIO_TOKEN")
            or os.environ.get("HA_SUPERVISOR_TOKEN")
            or opt_llt
            or os.environ.get("HOMEASSISTANT_TOKEN")
            or ""
        )
        ha_url = (
            os.environ.get("HA_URL")  # dev override
            or opts.get("ha_url")
            or "http://supervisor/core"
        )
        devices_path = _opts_str(opts, "devices_config_path", "SHAKIRA_DEVICES_PATH")
        if not devices_path:
            devices_path = "/homeassistant/shakira_devices.yaml"
        cameras_path = _opts_str(opts, "frigate_cameras_config_path", "SHAKIRA_CAMERAS_PATH")
        if not cameras_path:
            cameras_path = "/homeassistant/shakira_cameras.yaml"
        alerts_path = _opts_str(opts, "alerts_config_path", "SHAKIRA_ALERTS_PATH")
        if not alerts_path:
            alerts_path = "/homeassistant/shakira_alerts.yaml"

        return cls(
            supervisor_token=token.strip(),
            ha_url=str(ha_url).rstrip("/"),
            evolution_base_url=_opts_str(opts, "evolution_base_url", "EVOLUTION_BASE_URL").rstrip("/"),
            evolution_api_key=_opts_str(opts, "evolution_api_key", "EVOLUTION_API_KEY"),
            gemini_api_key=_opts_str(opts, "gemini_api_key", "GEMINI_API_KEY"),
            evolution_instance=_opts_str(opts, "evolution_instance", "EVOLUTION_INSTANCE"),
            devices_config_path=devices_path,
            gemini_cache_ttl_hours=_opts_int(opts, "gemini_cache_ttl_hours", 24),
            photoprism_url=_opts_str(opts, "photoprism_url", "PHOTOPRISM_URL").rstrip("/"),
            photoprism_token=_opts_str(opts, "photoprism_token", "PHOTOPRISM_TOKEN"),
            photoprism_max_photos=min(10, max(1, _opts_int(opts, "photoprism_max_photos", 10))),
            photoprism_api_prefix=_opts_str(opts, "photoprism_api_prefix", "PHOTOPRISM_API_PREFIX"),
            frigate_url=_opts_str(opts, "frigate_url", "FRIGATE_URL").rstrip("/"),
            frigate_cameras_config_path=cameras_path,
            alerts_config_path=alerts_path,
            shakira_api_token=_opts_str(opts, "shakira_api_token", "SHAKIRA_API_TOKEN"),
            log_level=_opts_log_level(opts),
        )

    @property
    def ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.supervisor_token}",
            "Content-Type": "application/json",
        }
