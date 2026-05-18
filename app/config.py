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


@dataclass
class AppSettings:
    """Configuracao em runtime."""

    supervisor_token: str
    ha_url: str
    evolution_instance_addon: str

    @classmethod
    def load(cls) -> AppSettings:
        opts = load_addon_options()
        token = (
            os.environ.get("SUPERVISOR_TOKEN")
            or os.environ.get("HA_SUPERVISOR_TOKEN")
            or os.environ.get("HOMEASSISTANT_TOKEN")
            or ""
        )
        ha_url = (
            os.environ.get("HA_URL")  # dev override
            or opts.get("ha_url")
            or "http://supervisor/core"
        )
        ev_instance = opts.get("evolution_instance") or os.environ.get("EVOLUTION_INSTANCE") or ""

        return cls(
            supervisor_token=token.strip(),
            ha_url=ha_url.rstrip("/"),
            evolution_instance_addon=ev_instance.strip(),
        )

    @property
    def ha_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.supervisor_token}",
            "Content-Type": "application/json",
        }
