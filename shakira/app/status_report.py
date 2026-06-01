"""Relatorio de status para dashboard e API JSON."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import AppSettings
from app.cameras_catalog import CamerasCatalog
from app.devices_catalog import DevicesCatalog
from app.frigate import FrigateClient
from app.whatsapp_phones import ENTITY_PERMITTED, fetch_permitted_phones_raw, parse_allowed_numbers
from app.homeassistant import HomeAssistantClient
from app.photoprism import PhotoprismClient
from app.message_timing import recent_averages
from app.user_data_migration import LEGACY_USER_DATA_ROOT, _list_phone_dirs
from app.user_memory import USER_DATA_ROOT

log = logging.getLogger(__name__)

VERSION = "1.7.83"


def _mask_secret(value: str, visible: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= visible:
        return "•" * len(v)
    return "•" * (len(v) - visible) + v[-visible:]


def _status_level(ok: bool, configured: bool = True) -> str:
    if not configured:
        return "disabled"
    return "ok" if ok else "error"


async def _check_home_assistant(ha: HomeAssistantClient, settings: AppSettings) -> dict[str, Any]:
    if not settings.supervisor_token:
        return {
            "id": "home_assistant",
            "name": "Home Assistant",
            "status": "error",
            "summary": "Token da API ausente",
            "details": {
                "url": settings.ha_url,
                "token": "nao configurado",
                "hint": "Defina homeassistant_long_lived_token nas opcoes do add-on.",
            },
        }
    try:
        r = await ha._client.get(
            f"{settings.ha_url}/api/",
            headers=settings.ha_headers,
            timeout=15.0,
        )
        ok = r.status_code == 200
        msg = "Conectado" if ok else f"HTTP {r.status_code}"
        permitted_count = None
        if ok:
            try:
                raw = await fetch_permitted_phones_raw(ha)
                permitted_count = len(parse_allowed_numbers(raw))
            except Exception:
                permitted_count = None
        return {
            "id": "home_assistant",
            "name": "Home Assistant",
            "status": _status_level(ok),
            "summary": msg,
            "details": {
                "url": settings.ha_url,
                "token": _mask_secret(settings.supervisor_token),
                "permitted_entity": ENTITY_PERMITTED,
                "permitted_phones_count": permitted_count,
                "states_cache_sec": settings.ha_states_cache_sec,
            },
        }
    except httpx.RequestError as e:
        return {
            "id": "home_assistant",
            "name": "Home Assistant",
            "status": "error",
            "summary": "Sem conexao",
            "details": {"url": settings.ha_url, "error": str(e)},
        }


async def _check_gemini(settings: AppSettings, cache_name: str | None) -> dict[str, Any]:
    key = settings.gemini_api_key.strip()
    if not key:
        return {
            "id": "gemini",
            "name": "Google Gemini",
            "status": "disabled",
            "summary": "Chave API nao configurada",
            "details": {"model": os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")},
        }

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    details: dict[str, Any] = {
        "model": model,
        "api_key": _mask_secret(key),
        "context_cache": cache_name or "inline (catalogo pequeno ou indisponivel)",
        "cache_ttl_hours": settings.gemini_cache_ttl_hours,
    }

    def _ping() -> tuple[bool, str]:
        try:
            import google.generativeai as genai

            genai.configure(api_key=key)
            # Listagem leve para validar credencial
            _ = next(genai.list_models(), None)
            return True, "API acessivel"
        except StopIteration:
            return True, "API acessivel"
        except Exception as e:
            return False, str(e)[:200]

    ok, msg = await asyncio.to_thread(_ping)
    return {
        "id": "gemini",
        "name": "Google Gemini",
        "status": _status_level(ok),
        "summary": msg,
        "details": details,
    }


async def _check_evolution(http: httpx.AsyncClient, settings: AppSettings) -> dict[str, Any]:
    base = settings.evolution_base_url.strip()
    api_key = settings.evolution_api_key.strip()
    instance = settings.evolution_instance.strip()
    if not base or not api_key:
        return {
            "id": "evolution",
            "name": "Evolution API (WhatsApp)",
            "status": "disabled",
            "summary": "URL ou API key nao configurados",
            "details": {"base_url": base or None, "instance": instance or None},
        }

    details: dict[str, Any] = {
        "base_url": base,
        "api_key": _mask_secret(api_key),
        "instance": instance or "(via webhook)",
    }
    try:
        url = f"{base.rstrip('/')}/"
        r = await http.get(
            url,
            headers={"apikey": api_key},
            timeout=15.0,
            follow_redirects=True,
        )
        ok = r.status_code < 500
        summary = f"HTTP {r.status_code}" if ok else f"Erro HTTP {r.status_code}"
        details["probe_url"] = url
        details["response_preview"] = (r.text or "")[:120]
        return {
            "id": "evolution",
            "name": "Evolution API (WhatsApp)",
            "status": "ok" if r.status_code in (200, 201, 404) else "warning" if ok else "error",
            "summary": summary,
            "details": details,
        }
    except httpx.RequestError as e:
        return {
            "id": "evolution",
            "name": "Evolution API (WhatsApp)",
            "status": "error",
            "summary": "Sem conexao",
            "details": {**details, "error": str(e)},
        }


async def _check_photoprism(http: httpx.AsyncClient, settings: AppSettings) -> dict[str, Any]:
    url = settings.photoprism_url.strip()
    token = settings.photoprism_token.strip()
    if not url or not token:
        return {
            "id": "photoprism",
            "name": "PhotoPrism",
            "status": "disabled",
            "summary": "Nao configurado",
            "details": {"max_photos_per_request": settings.photoprism_max_photos},
        }

    pp = PhotoprismClient(
        http,
        base_url=url,
        token=token,
        api_prefix=settings.photoprism_api_prefix,
    )
    try:
        probe = await pp.probe(supervisor_token=settings.supervisor_token)
        summary_map = {
            "ok": "API acessivel",
            "nginx_proxy?": "URL aponta para nginx (nao PhotoPrism)",
            "ingress_sem_api": "Ingress detectado mas API nao responde",
            "unreachable_or_wrong_url": "URL inacessivel ou incorreta",
        }
        summary = summary_map.get(probe.get("summary", ""), str(probe.get("summary")))
        status = "ok" if probe.get("summary") == "ok" else "error"
        return {
            "id": "photoprism",
            "name": "PhotoPrism",
            "status": status,
            "summary": summary,
            "details": {
                "url": url,
                "api_base": probe.get("api_base"),
                "api_prefix": settings.photoprism_api_prefix or None,
                "token": _mask_secret(token),
                "max_photos": settings.photoprism_max_photos,
                "hints": probe.get("hints") or [],
                "probes": probe.get("probes") or [],
            },
        }
    except Exception as e:
        return {
            "id": "photoprism",
            "name": "PhotoPrism",
            "status": "error",
            "summary": str(e)[:120],
            "details": {"url": url},
        }


def _check_catalog(catalog: DevicesCatalog, settings: AppSettings) -> dict[str, Any]:
    resolved = catalog.source_path
    exists = bool(resolved and resolved.is_file())
    actionable = catalog.actionable_entity_ids()
    if not exists:
        status, summary = "error", "Arquivo de catalogo nao encontrado"
    elif not catalog.devices:
        status, summary = "warning", "Catalogo vazio — nenhuma acao permitida"
    else:
        status, summary = "ok", f"{len(catalog.devices)} dispositivo(s), {len(actionable)} acionavel(is)"

    return {
        "id": "catalog",
        "name": "Catalogo de dispositivos",
        "status": status,
        "summary": summary,
        "details": {
            "path": settings.devices_config_path,
            "resolved_path": str(resolved) if resolved else None,
            "file_exists": exists,
            "devices_count": len(catalog.devices),
            "actionable_entities": sorted(actionable),
        },
    }


def _check_webhook() -> dict[str, Any]:
    return {
        "id": "webhook",
        "name": "Webhook Evolution",
        "status": "ok",
        "summary": "POST /webhook (porta 8099)",
        "details": {
            "path": "/webhook",
            "note": "Configure a automacao HA para encaminhar eventos MESSAGES_UPSERT.",
        },
    }


def _check_presence_simulator(status: dict[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {
            "id": "presence_simulator",
            "name": "Simulador de presença",
            "status": "disabled",
            "summary": "Não configurado ou desativado",
            "details": {},
        }
    
    enabled = status.get("enabled", False)
    running = status.get("running", False)
    
    if not enabled:
        level = "disabled"
        summary = "Desativado nas configurações"
    elif running:
        level = "ok"
        active_light = status.get("active_light")
        if active_light:
            summary = f"Ligado (Luz ativa no momento: {active_light})"
        else:
            summary = "Ligado (Aguardando próximo acionamento)"
    else:
        level = "warning"
        summary = "Ativo nas configurações, mas inativo no Home Assistant"
        
    return {
        "id": "presence_simulator",
        "name": "Simulador de presença",
        "status": level,
        "summary": summary,
        "details": status,
    }


def _check_user_data(
    settings: AppSettings,
    *,
    user_data_migrated_phones: list[str] | None = None,
) -> dict[str, Any]:
    root = USER_DATA_ROOT
    exists = root.is_dir()
    dest_phones = [p.name for p in _list_phone_dirs(root)] if exists else []
    legacy_phones = [p.name for p in _list_phone_dirs(LEGACY_USER_DATA_ROOT)]
    pending_legacy = [p for p in legacy_phones if p not in dest_phones]
    ha_path = "/config/shakira_users"
    if str(root).startswith("/homeassistant"):
        ha_path = "/config" + str(root)[len("/homeassistant") :]

    if not exists:
        status, summary = "warning", "Pasta de dados inacessivel"
    elif pending_legacy:
        status = "warning"
        summary = (
            f"{len(dest_phones)} utilizador(es) activos; "
            f"legado em /data ainda tem {len(pending_legacy)} telefone(s) por migrar"
        )
    elif dest_phones:
        status, summary = "ok", f"Pasta activa ({len(dest_phones)} utilizador(es))"
    else:
        status, summary = "ok", "Pasta activa (vazia)"

    return {
        "id": "user_data",
        "name": "Dados por utilizador",
        "status": status,
        "summary": summary,
        "details": {
            "configured_path": settings.user_data_path,
            "resolved_path": str(root),
            "ha_config_path_hint": ha_path,
            "legacy_path": str(LEGACY_USER_DATA_ROOT),
            "legacy_phones": legacy_phones,
            "dest_phones": dest_phones,
            "pending_legacy_phones": pending_legacy,
            "migrated_phones_this_start": list(user_data_migrated_phones or []),
            "user_dirs": len(dest_phones),
        },
    }


def _check_scheduled_responses(status: dict[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {
            "id": "scheduled_responses",
            "name": "Respostas agendadas",
            "status": "disabled",
            "summary": "Executor nao inicializado",
            "details": {},
        }
    pending = int(status.get("pending_count") or 0)
    running = bool(status.get("running"))
    if pending == 0:
        summary = "Nenhum agendamento pending"
        level = "ok"
    elif running:
        summary = f"{pending} agendamento(s) pending, executor activo"
        level = "ok"
    else:
        summary = f"{pending} agendamento(s) pending, executor parado"
        level = "warning"
    return {
        "id": "scheduled_responses",
        "name": "Respostas agendadas",
        "status": level,
        "summary": summary,
        "details": status,
    }


async def _check_frigate(
    http: httpx.AsyncClient, settings: AppSettings, cameras: CamerasCatalog
) -> dict[str, Any]:
    url = settings.frigate_url.strip()
    if not url:
        return {
            "id": "frigate",
            "name": "Frigate (cameras)",
            "status": "disabled",
            "summary": "Nao configurado",
            "details": {"cameras_config_path": settings.frigate_cameras_config_path},
        }

    frigate = FrigateClient(http, base_url=url)
    probe = await frigate.probe()
    reachable = bool(probe.get("reachable"))
    cam_count = len(cameras.cameras)
    if not reachable:
        status, summary = "error", "Frigate inacessivel"
    elif not cameras.cameras:
        status, summary = "warning", "Conectado mas catalogo de cameras vazio"
    else:
        status, summary = "ok", f"Conectado, {cam_count} camera(s) no catalogo"

    return {
        "id": "frigate",
        "name": "Frigate (cameras)",
        "status": status if url else "disabled",
        "summary": summary,
        "details": {
            "url": url,
            "cameras_count": cam_count,
            "cameras_config_path": settings.frigate_cameras_config_path,
            "camera_ids": [c.id for c in cameras.cameras],
            "probe": probe,
        },
    }


async def build_status_report(
    *,
    settings: AppSettings,
    http: httpx.AsyncClient,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog | None = None,
    gemini_cache_name: str | None,
    started_at: float | None = None,
    scheduled_responses_status: dict[str, Any] | None = None,
    user_data_migrated_phones: list[str] | None = None,
    presence_simulator_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if cameras is None:
        cameras = CamerasCatalog.load(settings.frigate_cameras_config_path)

    checks = await asyncio.gather(
        _check_home_assistant(ha, settings),
        _check_gemini(settings, gemini_cache_name),
        _check_evolution(http, settings),
        _check_photoprism(http, settings),
        _check_frigate(http, settings, cameras),
        return_exceptions=True,
    )

    services: list[dict[str, Any]] = []
    for item in checks:
        if isinstance(item, Exception):
            log.exception("Erro em check de servico: %s", item)
            services.append(
                {
                    "id": "unknown",
                    "name": "Erro interno",
                    "status": "error",
                    "summary": str(item)[:200],
                    "details": {},
                }
            )
        else:
            services.append(item)

    services.append(_check_catalog(catalog, settings))
    services.append(
        _check_user_data(settings, user_data_migrated_phones=user_data_migrated_phones)
    )
    services.append(_check_webhook())
    services.append(_check_scheduled_responses(scheduled_responses_status))
    services.append(_check_presence_simulator(presence_simulator_status))

    uptime_s = None
    if started_at:
        uptime_s = int(time.time() - started_at)

    overall = "ok"
    if any(s["status"] == "error" for s in services):
        overall = "error"
    elif any(s["status"] == "warning" for s in services):
        overall = "warning"

    return {
        "version": VERSION,
        "service": "shakira",
        "overall": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": uptime_s,
        "services": services,
        "scheduled_pending": (
            list(scheduled_responses_status.get("pending_items") or [])
            if scheduled_responses_status
            else []
        ),
        "performance": recent_averages(),
        "log_level": settings.log_level,
    }
