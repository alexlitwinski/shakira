"""Carrega configuracao do Supervisor e options.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass
class AppSettings:
    """Configuracao em runtime."""

    supervisor_token: str
    ha_url: str
    evolution_base_url: str
    evolution_api_key: str
    gemini_api_key: str
    evolution_instance: str

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

        return cls(
            supervisor_token=token.strip(),
            ha_url=str(ha_url).rstrip("/"),
            evolution_base_url=_opts_str(opts, "evolution_base_url", "EVOLUTION_BASE_URL").rstrip("/"),
            evolution_api_key=_opts_str(opts, "evolution_api_key", "EVOLUTION_API_KEY"),
            gemini_api_key=_opts_str(opts, "gemini_api_key", "GEMINI_API_KEY"),
            evolution_instance=_opts_str(opts, "evolution_instance", "EVOLUTION_INSTANCE"),
        )

    @property
    def ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.supervisor_token}",
            "Content-Type": "application/json",
        }
