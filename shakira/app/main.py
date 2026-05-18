"""API FastAPI: webhook Evolution + health + painel Ingress."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import AppSettings
from app.dashboard import get_dashboard_html
from app.devices_catalog import DevicesCatalog
from app.devices_catalog import CatalogValidationError
from app.devices_yaml_io import read_yaml_file, validate_yaml_content, write_yaml_file
from app.evolution import EvolutionClient
from app.gemini_cache import ensure_catalog_cache
from app.handlers import handle_evolution_payload
from app.homeassistant import HomeAssistantClient
from app.status_report import build_status_report

_log_level_name = os.environ.get("SHAKIRA_LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("shakira")
log.info("Nivel de log: %s", logging.getLevelName(_log_level))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppSettings.load()
    app.state.settings = settings
    app.state.started_at = time.time()

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

    catalog = DevicesCatalog.load(settings.devices_config_path)
    app.state.catalog = catalog
    cache_name = None
    if settings.gemini_api_key and catalog.devices:
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


async def _status_payload(request: Request) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    catalog: DevicesCatalog = getattr(
        request.app.state,
        "catalog",
        DevicesCatalog.load(settings.devices_config_path),
    )
    http: httpx.AsyncClient = request.app.state.http
    ha: HomeAssistantClient = request.app.state.ha
    started = getattr(request.app.state, "started_at", None)
    return await build_status_report(
        settings=settings,
        http=http,
        ha=ha,
        catalog=catalog,
        gemini_cache_name=getattr(request.app.state, "gemini_cache_name", None),
        started_at=started,
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Interface web do add-on (Ingress): painel de status."""
    return HTMLResponse(content=get_dashboard_html())


@app.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    """JSON com testes de conexao (usado pelo painel)."""
    return await _status_payload(request)


@app.get("/status")
async def status_legacy(request: Request) -> dict[str, Any]:
    """Alias legado do relatorio de status."""
    return await _status_payload(request)


class DevicesYamlBody(BaseModel):
    content: str = Field(..., min_length=1)


@app.get("/api/devices-yaml")
async def get_devices_yaml(request: Request) -> dict[str, Any]:
    """Conteudo do shakira_devices.yaml para o editor do painel."""
    settings: AppSettings = request.app.state.settings
    return read_yaml_file(settings.devices_config_path)


@app.post("/api/devices-yaml/validate")
async def post_devices_yaml_validate(body: DevicesYamlBody) -> dict[str, Any]:
    """Valida estrutura sem gravar (para o editor do painel)."""
    try:
        errors = validate_yaml_content(body.content)
    except ValueError as e:
        errors = [str(e)]
    return {"valid": not errors, "errors": errors}


@app.put("/api/devices-yaml")
async def put_devices_yaml(request: Request, body: DevicesYamlBody) -> dict[str, Any]:
    """Grava shakira_devices.yaml e recarrega o catalogo em memoria."""
    settings: AppSettings = request.app.state.settings
    try:
        result = write_yaml_file(settings.devices_config_path, body.content)
    except CatalogValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"errors": [str(e)]}) from e

    catalog = DevicesCatalog.load(settings.devices_config_path)
    request.app.state.catalog = catalog
    result["message"] = (
        "Arquivo salvo. O catalogo foi recarregado; reinicie o add-on se alterou cenarios "
        "e o assistente nao refletir na hora."
    )
    return result


async def _run_webhook(
    body: Any,
    settings: AppSettings,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    gemini_cache_name: str | None,
    http: httpx.AsyncClient,
) -> None:
    try:
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    await handle_evolution_payload(
                        item,
                        ha=ha,
                        evo=evo,
                        settings=settings,
                        gemini_cache_name=gemini_cache_name,
                        http=http,
                    )
        elif isinstance(body, dict):
            ev = str(body.get("event") or "").upper()
            if ev and "UPSERT" not in ev:
                log.debug("Evento ignorado: %s", ev)
                return
            await handle_evolution_payload(
                body,
                ha=ha,
                evo=evo,
                settings=settings,
                gemini_cache_name=gemini_cache_name,
                http=http,
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

    http: httpx.AsyncClient = request.app.state.http
    background_tasks.add_task(_run_webhook, body, settings, ha, evo, cache_name, http)
    return {"ok": "true"}
