"""API FastAPI: webhook Evolution + health."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import AppSettings
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.gemini_cache import ensure_catalog_cache
from app.handlers import handle_evolution_payload
from app.homeassistant import HomeAssistantClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("shakira")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppSettings.load()
    app.state.settings = settings

    if not settings.supervisor_token:
        log.warning(
            "Nenhum token da API encontrado (SUPERVISOR_TOKEN vazio no container). "
            "Nas configuracoes do add-on Shakira defina homeassistant_long_lived_token "
            "(Perfil HA > Tokens de longa duracao) ou reinstalle/reinicie depois de "
            "guardar homeassistant_api: true."
        )

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
    client = httpx.AsyncClient(timeout=timeout)
    app.state.http = client
    app.state.ha = HomeAssistantClient(settings, client)
    app.state.evo = EvolutionClient(client)
    app.state.settings = settings

    catalog = DevicesCatalog.load(settings.devices_config_path)
    app.state.catalog = catalog
    cache_name = None
    if settings.gemini_api_key and catalog.devices:
        import os

        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        cache_name = ensure_catalog_cache(
            api_key=settings.gemini_api_key,
            model=model,
            catalog=catalog,
            ttl_hours=settings.gemini_cache_ttl_hours,
        )
    elif settings.gemini_api_key and not catalog.devices:
        log.warning(
            "Catalogo vazio — nenhuma acao permitida. Crie %s (veja log acima se ficheiro ausente).",
            settings.devices_config_path,
        )
    app.state.gemini_cache_name = cache_name

    yield

    await client.aclose()


app = FastAPI(title="Shakira WhatsApp Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "shakira", "status": "ok"}


@app.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Diagnostico: catalogo de dispositivos e cache Gemini."""
    settings: AppSettings = request.app.state.settings
    catalog: DevicesCatalog = getattr(request.app.state, "catalog", DevicesCatalog.load(settings.devices_config_path))
    path = settings.devices_config_path
    resolved = catalog.source_path
    exists = bool(resolved and resolved.is_file())
    actionable = sorted(catalog.actionable_entity_ids())
    return {
        "version": "1.0.6",
        "devices_config_path": path,
        "file_exists": exists,
        "resolved_path": str(resolved) if resolved else None,
        "devices_count": len(catalog.devices),
        "actionable_entities": actionable,
        "gemini_cache": getattr(request.app.state, "gemini_cache_name", None),
    }


async def _run_webhook(
    body: Any,
    settings: AppSettings,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    gemini_cache_name: str | None,
) -> None:
    try:
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    await handle_evolution_payload(
                        item, ha=ha, evo=evo, settings=settings, gemini_cache_name=gemini_cache_name
                    )
        elif isinstance(body, dict):
            ev = str(body.get("event") or "").upper()
            if ev and "UPSERT" not in ev:
                log.debug("Evento ignorado: %s", ev)
                return
            await handle_evolution_payload(
                body, ha=ha, evo=evo, settings=settings, gemini_cache_name=gemini_cache_name
            )
        else:
            log.warning("Payload webhook inesperado: %s", type(body))
    except Exception:
        log.exception("Erro ao processar webhook Evolution")


@app.post("/webhook")
async def evolution_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            body = await request.json()
        else:
            raw = await request.body()
            if not raw:
                raise ValueError("corpo vazio")
            body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("JSON invalido no webhook: %s", e)
        raise HTTPException(status_code=400, detail="JSON invalido") from e

    settings: AppSettings = request.app.state.settings
    ha: HomeAssistantClient = request.app.state.ha
    evo: EvolutionClient = request.app.state.evo
    cache_name: str | None = getattr(request.app.state, "gemini_cache_name", None)

    background_tasks.add_task(_run_webhook, body, settings, ha, evo, cache_name)
    return {"ok": "true"}
