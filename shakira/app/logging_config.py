"""Configuracao centralizada de logging do add-on Shakira."""

from __future__ import annotations

import logging
import sys

VALID_LOG_LEVELS = frozenset({"none", "error", "info", "debug"})

# Acima de CRITICAL — silencia tudo quando nivel = none
_LEVEL_NONE = 100

_DEBUG_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "uvicorn.access",
)

# Bibliotecas cujo DEBUG e ruido (frames WS, etc.) — nunca util para o utilizador
_ALWAYS_QUIET_LOGGERS = (
    "websockets",
    "websockets.client",
    "websockets.server",
    "websockets.protocol",
)


def normalize_log_level(raw: str | None) -> str:
    level = (raw or "info").strip().lower()
    if level in VALID_LOG_LEVELS:
        return level
    alias = {
        "warn": "error",
        "warning": "error",
        "critical": "error",
        "off": "none",
        "silent": "none",
    }
    return alias.get(level, "info")


def resolve_logging_level(level_name: str) -> int:
    name = normalize_log_level(level_name)
    if name == "none":
        return _LEVEL_NONE
    return {
        "error": logging.ERROR,
        "info": logging.INFO,
        "debug": logging.DEBUG,
    }[name]


def is_debug_enabled(level_name: str | None = None) -> bool:
    if level_name is None:
        root = logging.getLogger("shakira")
        return root.isEnabledFor(logging.DEBUG)
    return normalize_log_level(level_name) == "debug"


def configure_logging(level_name: str) -> str:
    """Aplica nivel de log global. Retorna o nivel normalizado (none|error|info|debug)."""
    normalized = normalize_log_level(level_name)
    level = resolve_logging_level(normalized)

    if normalized == "none":
        logging.disable(_LEVEL_NONE)
    else:
        logging.disable(logging.NOTSET)

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stdout,
        )
    else:
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)

    for name in ("shakira", "app"):
        logging.getLogger(name).setLevel(level)

    for name in _DEBUG_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(
            logging.DEBUG if normalized == "debug" else logging.WARNING
        )

    for name in _ALWAYS_QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("uvicorn.error").setLevel(
        logging.DEBUG if normalized == "debug" else logging.INFO
    )

    log = logging.getLogger("shakira")
    if normalized == "none":
        log.debug("Logging desativado (nivel none)")
    else:
        log.info("Nivel de log: %s", normalized)
        if normalized == "debug":
            log.debug(
                "Modo debug: logs de performance, cache HA, Gemini, webhooks e HTTP activos"
            )

    return normalized
