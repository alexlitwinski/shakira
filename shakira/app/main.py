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

from app.config import AppSettings, load_addon_options
from app.logging_config import configure_logging
from app.dashboard import get_dashboard_html
from app.cameras_catalog import CamerasCatalog, CamerasCatalogValidationError
from app.devices_catalog import CatalogValidationError, DevicesCatalog
from app.alerts_catalog import AlertsCatalog, AlertsCatalogValidationError
from app.alerts_runner import AlertsRunner
from app.scheduled_responses import count_all_pending_globally
from app.scheduled_responses_runner import ScheduledResponsesRunner, set_scheduled_runner
from app.alerts_yaml_io import (
    read_yaml_file as read_alerts_yaml_file,
    validate_yaml_content as validate_alerts_yaml_content,
    write_yaml_file as write_alerts_yaml_file,
)
from app.cameras_yaml_io import (
    read_yaml_file as read_cameras_yaml_file,
    validate_yaml_content as validate_cameras_yaml_content,
    write_yaml_file as write_cameras_yaml_file,
)
from app.devices_yaml_io import read_yaml_file, validate_yaml_content, write_yaml_file
from app.evolution import EvolutionClient
from app.gemini_cache import ensure_catalog_cache
from app.handlers import handle_evolution_payload
from app.homeassistant import HomeAssistantClient
from app.status_report import build_status_report
from app.whatsapp_outbound import WhatsAppSendError, send_whatsapp_text

_opts_boot = load_addon_options()
_boot_level = _opts_boot.get("log_level") if isinstance(_opts_boot.get("log_level"), str) else None
if not _boot_level:
    _boot_level = os.environ.get("SHAKIRA_LOG_LEVEL", "info")
configure_logging(str(_boot_level))
log = logging.getLogger("shakira")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppSettings.load()
    configure_logging(settings.log_level)
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
    cameras = CamerasCatalog.load(settings.frigate_cameras_config_path)
    app.state.catalog = catalog
    app.state.cameras = cameras
    cache_name = None
    if settings.gemini_api_key and (catalog.devices or cameras.cameras):
        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        cache_name = ensure_catalog_cache(
            api_key=settings.gemini_api_key,
            model=model,
            catalog=catalog,
            cameras=cameras,
            ttl_hours=settings.gemini_cache_ttl_hours,
        )
    elif settings.gemini_api_key and not catalog.devices:
        log.warning(
            "Catalogo vazio — nenhuma acao permitida. Crie %s (veja log acima se ficheiro ausente).",
            settings.devices_config_path,
        )
    app.state.gemini_cache_name = cache_name

    alerts = AlertsCatalog.load(settings.alerts_config_path)
    app.state.alerts = alerts
    alerts_runner = AlertsRunner(
        settings=settings,
        ha=app.state.ha,
        evo=app.state.evo,
        catalog=alerts,
        cameras=cameras,
        http=client,
    )
    if alerts.enabled_alerts():
        alerts_runner.start()
    else:
        log.info(
            "Nenhum alerta ativo em %s — executor de alertas em espera",
            settings.alerts_config_path,
        )
    app.state.alerts_runner = alerts_runner

    scheduled_runner = ScheduledResponsesRunner(
        settings=settings,
        ha=app.state.ha,
        evo=app.state.evo,
        catalog=catalog,
        cameras=cameras,
        gemini_cache_name=cache_name,
    )
    app.state.scheduled_runner = scheduled_runner
    set_scheduled_runner(scheduled_runner)
    if count_all_pending_globally():
        scheduled_runner.start()
    else:
        log.info("Nenhum agendamento pending — executor de respostas agendadas em espera")

    yield

    await scheduled_runner.stop()
    await alerts_runner.stop()
    await client.aclose()


app = FastAPI(title="Shakira WhatsApp Bot", lifespan=lifespan)


async def _status_payload(request: Request) -> dict[str, Any]:
    settings: AppSettings = request.app.state.settings
    catalog: DevicesCatalog = getattr(
        request.app.state,
        "catalog",
        DevicesCatalog.load(settings.devices_config_path),
    )
    cameras: CamerasCatalog = getattr(
        request.app.state,
        "cameras",
        CamerasCatalog.load(settings.frigate_cameras_config_path),
    )
    http: httpx.AsyncClient = request.app.state.http
    ha: HomeAssistantClient = request.app.state.ha
    started = getattr(request.app.state, "started_at", None)
    scheduled_runner: ScheduledResponsesRunner | None = getattr(
        request.app.state, "scheduled_runner", None
    )
    scheduled_status = scheduled_runner.status_snapshot() if scheduled_runner else None
    return await build_status_report(
        settings=settings,
        http=http,
        ha=ha,
        catalog=catalog,
        cameras=cameras,
        gemini_cache_name=getattr(request.app.state, "gemini_cache_name", None),
        started_at=started,
        scheduled_responses_status=scheduled_status,
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


class YamlEditorBody(BaseModel):
    content: str = Field(..., min_length=1)


DevicesYamlBody = YamlEditorBody
CamerasYamlBody = YamlEditorBody
AlertsYamlBody = YamlEditorBody


def _refresh_gemini_cache(request: Request) -> None:
    settings: AppSettings = request.app.state.settings
    if not settings.gemini_api_key.strip():
        return
    catalog: DevicesCatalog = request.app.state.catalog
    cameras = CamerasCatalog.load(settings.frigate_cameras_config_path)
    request.app.state.cameras = cameras
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    request.app.state.gemini_cache_name = ensure_catalog_cache(
        api_key=settings.gemini_api_key,
        model=model,
        catalog=catalog,
        cameras=cameras,
        ttl_hours=settings.gemini_cache_ttl_hours,
    )


class WhatsAppSendBody(BaseModel):
    number: str = Field(..., min_length=8, description="Telefone (DDI+DDD+numero, somente digitos)")
    message: str = Field(..., min_length=1, description="Texto da mensagem")
    instance: str | None = Field(None, description="Instancia Evolution (opcional)")


def _check_shakira_api_token(request: Request, settings: AppSettings) -> None:
    expected = settings.shakira_api_token.strip()
    if not expected:
        log.warning("POST /api/whatsapp/send sem shakira_api_token configurado (endpoint aberto)")
        return
    auth = request.headers.get("Authorization", "").strip()
    header_token = request.headers.get("X-Shakira-Token", "").strip()
    if auth == f"Bearer {expected}" or auth == expected:
        return
    if header_token == expected:
        return
    raise HTTPException(status_code=401, detail="Token invalido ou ausente")


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


@app.get("/api/cameras-yaml")
async def get_cameras_yaml(request: Request) -> dict[str, Any]:
    """Conteudo do shakira_cameras.yaml para o editor do painel."""
    settings: AppSettings = request.app.state.settings
    return read_cameras_yaml_file(settings.frigate_cameras_config_path)


@app.post("/api/cameras-yaml/validate")
async def post_cameras_yaml_validate(body: CamerasYamlBody) -> dict[str, Any]:
    """Valida estrutura sem gravar."""
    try:
        errors = validate_cameras_yaml_content(body.content)
    except ValueError as e:
        errors = [str(e)]
    return {"valid": not errors, "errors": errors}


@app.put("/api/cameras-yaml")
async def put_cameras_yaml(request: Request, body: CamerasYamlBody) -> dict[str, Any]:
    """Grava shakira_cameras.yaml e recarrega catalogo de cameras."""
    settings: AppSettings = request.app.state.settings
    try:
        result = write_cameras_yaml_file(settings.frigate_cameras_config_path, body.content)
    except CamerasCatalogValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"errors": [str(e)]}) from e

    _refresh_gemini_cache(request)
    runner: AlertsRunner | None = getattr(request.app.state, "alerts_runner", None)
    if runner is not None:
        runner.cameras = request.app.state.cameras
    result["message"] = (
        "Arquivo salvo. Catalogo de cameras recarregado; cache Gemini atualizado."
    )
    return result


@app.get("/api/alerts-yaml")
async def get_alerts_yaml(request: Request) -> dict[str, Any]:
    """Conteudo do shakira_alerts.yaml para o editor do painel."""
    settings: AppSettings = request.app.state.settings
    return read_alerts_yaml_file(settings.alerts_config_path)


@app.post("/api/alerts-yaml/validate")
async def post_alerts_yaml_validate(body: AlertsYamlBody) -> dict[str, Any]:
    """Valida estrutura sem gravar."""
    try:
        errors = validate_alerts_yaml_content(body.content)
    except ValueError as e:
        errors = [str(e)]
    return {"valid": not errors, "errors": errors}


@app.put("/api/alerts-yaml")
async def put_alerts_yaml(request: Request, body: AlertsYamlBody) -> dict[str, Any]:
    """Grava shakira_alerts.yaml e recarrega o executor de alertas."""
    settings: AppSettings = request.app.state.settings
    try:
        result = write_alerts_yaml_file(settings.alerts_config_path, body.content)
    except AlertsCatalogValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"errors": [str(e)]}) from e

    catalog = AlertsCatalog.load(settings.alerts_config_path)
    request.app.state.alerts = catalog
    runner: AlertsRunner = request.app.state.alerts_runner
    runner.reload(catalog)
    await runner.ensure_running()
    result["message"] = (
        "Arquivo salvo. Alertas periodicos e live recarregados."
    )
    return result


@app.get("/api/alerts/status")
async def get_alerts_status(request: Request) -> dict[str, Any]:
    """Estado do executor de alertas (painel / diagnostico)."""
    runner: AlertsRunner = request.app.state.alerts_runner
    return runner.status_snapshot()


@app.get("/api/scheduled-responses/status")
async def get_scheduled_responses_status(request: Request) -> dict[str, Any]:
    """Estado do executor de respostas agendadas (painel / diagnostico)."""
    runner: ScheduledResponsesRunner = request.app.state.scheduled_runner
    return runner.status_snapshot()


async def _run_webhook(
    body: Any,
    settings: AppSettings,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    gemini_cache_name: str | None,
    http: httpx.AsyncClient,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog,
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
                        catalog=catalog,
                        cameras=cameras,
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
                catalog=catalog,
                cameras=cameras,
            )
            log.debug("Webhook Evolution processado event=%s", ev)
        else:
            log.warning("Payload webhook inesperado: %s", type(body))
    except Exception:
        log.exception("Erro ao processar webhook Evolution")


@app.post("/api/whatsapp/send")
async def api_whatsapp_send(request: Request, body: WhatsAppSendBody) -> dict[str, Any]:
    """Envia mensagem WhatsApp (para rest_command / automacoes do Home Assistant)."""
    settings: AppSettings = request.app.state.settings
    _check_shakira_api_token(request, settings)
    evo: EvolutionClient = request.app.state.evo
    try:
        return await send_whatsapp_text(
            settings=settings,
            evo=evo,
            number=body.number,
            message=body.message,
            instance=body.instance,
        )
    except WhatsAppSendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
    catalog: DevicesCatalog = request.app.state.catalog
    cameras: CamerasCatalog = request.app.state.cameras

    http: httpx.AsyncClient = request.app.state.http
    background_tasks.add_task(
        _run_webhook, body, settings, ha, evo, cache_name, http, catalog, cameras
    )
    return {"ok": "true"}
