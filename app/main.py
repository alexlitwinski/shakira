"""API FastAPI: webhook Evolution + health."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import AppSettings
from app.evolution import EvolutionClient
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
        log.error(
            "SUPERVISOR_TOKEN ausente. Habilitacao homeassistant_api e reinicio necessarios. "
            "Para teste local use HOMEASSISTANT_TOKEN e HA_URL."
        )

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
    client = httpx.AsyncClient(timeout=timeout)
    app.state.http = client
    app.state.ha = HomeAssistantClient(settings, client)
    app.state.evo = EvolutionClient(client)

    yield

    await client.aclose()


app = FastAPI(title="Shakira WhatsApp Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "shakira", "status": "ok"}


async def _run_webhook(body: Any, settings: AppSettings, ha: HomeAssistantClient, evo: EvolutionClient) -> None:
    try:
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    await handle_evolution_payload(item, ha=ha, evo=evo, settings=settings)
        elif isinstance(body, dict):
            ev = str(body.get("event") or "").upper()
            if ev and "UPSERT" not in ev:
                log.debug("Evento ignorado: %s", ev)
                return
            await handle_evolution_payload(body, ha=ha, evo=evo, settings=settings)
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

    background_tasks.add_task(_run_webhook, body, settings, ha, evo)
    return {"ok": "true"}
