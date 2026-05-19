"""Envio de snapshots Frigate para WhatsApp (uma ou varias cameras)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.frigate import FrigateClient, FrigateError
from app.whatsapp_steps import StepMessenger, pulse_whatsapp_typing, truncate_whatsapp

log = logging.getLogger(__name__)

_SNAPSHOT_DELAY_SEC = float(os.environ.get("CAMERA_SNAPSHOT_DELAY_SEC", "0.3"))


@dataclass
class CameraSnapshotsResult:
    sent: int = 0
    failed: list[str] = field(default_factory=list)
    summary: str = ""


def parse_camera_snapshot_targets(
    decision: dict[str, Any],
    cameras: CamerasCatalog,
) -> tuple[list[str], str | None]:
    """Extrai filtros do JSON do Gemini e resolve ids de camera."""
    all_cameras = decision.get("all_cameras") is True

    raw_ids = decision.get("camera_ids")
    camera_ids: list[str] | None = None
    if isinstance(raw_ids, list):
        camera_ids = [str(x) for x in raw_ids if str(x).strip()]
    elif isinstance(raw_ids, str) and raw_ids.strip():
        camera_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]

    camera_group = decision.get("camera_group")
    group_str = str(camera_group).strip() if isinstance(camera_group, str) else None

    raw_id = decision.get("camera_id")
    camera_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None

    return cameras.resolve_camera_targets(
        camera_id=camera_id,
        camera_ids=camera_ids,
        camera_group=group_str,
        all_cameras=all_cameras,
    )


async def send_camera_snapshots(
    *,
    settings: AppSettings,
    cameras: CamerasCatalog,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    camera_ids: list[str],
    intro: str = "",
    messenger: StepMessenger | None = None,
) -> CameraSnapshotsResult:
    """
    Obtem snapshots do Frigate e envia pelo WhatsApp.
    Reutilizavel por codigo ou pelo agente (via handle_get_camera_snapshot).
    """
    result = CameraSnapshotsResult()
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()

    async def say(text: str, *, final: bool = False) -> None:
        msg = truncate_whatsapp(text)
        if not msg:
            return
        if messenger:
            await messenger.step(msg, final=final)
        elif evo_base and evo_key and instance:
            await pulse_whatsapp_typing()
            await evo.send_text(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                text=msg,
            )

    if not evo_base or not evo_key or not instance:
        result.summary = "Evolution nao configurado."
        log.error("Envio de cameras bloqueado: Evolution nao configurado")
        return result

    if not settings.frigate_url:
        result.summary = "Frigate nao configurado."
        if intro:
            await say(intro)
        await say("Frigate nao configurado. Defina frigate_url nas opcoes do add-on.", final=True)
        return result

    if not cameras.cameras:
        result.summary = "Nenhuma camera configurada."
        if intro:
            await say(intro)
        await say("Nenhuma camera configurada. Crie /config/shakira_cameras.yaml.", final=True)
        return result

    if not camera_ids:
        known = ", ".join(f"{c.id} ({c.name})" for c in cameras.cameras[:8])
        groups = ", ".join(cameras.list_groups())
        hint = known
        if groups:
            hint += f". Grupos: {groups}"
        result.summary = "Nenhuma camera selecionada."
        if intro:
            await say(intro)
        await say(f"Nao identifiquei quais cameras enviar. Disponiveis: {hint}.", final=True)
        return result

    if intro:
        await say(intro)

    total = len(camera_ids)
    if total == 1:
        cam = cameras.camera_map().get(camera_ids[0])
        label = cam.name if cam else camera_ids[0]
        await say(f"Vou buscar a imagem da camera {label}...")
    else:
        await say(f"Vou buscar imagens de {total} cameras...")

    frigate = FrigateClient(http, base_url=settings.frigate_url)
    cam_map = cameras.camera_map()

    for index, camera_id in enumerate(camera_ids, start=1):
        cam = cam_map.get(camera_id)
        label = cam.name if cam else camera_id
        log.info(
            "Frigate snapshot camera=%s (%s/%s) url=%s",
            camera_id,
            index,
            total,
            settings.frigate_url,
        )

        try:
            image_bytes = await frigate.get_latest_snapshot(camera_id)
        except FrigateError as e:
            log.error("Frigate falhou camera=%s: %s", camera_id, e, exc_info=True)
            result.failed.append(camera_id)
            if total > 1:
                await say(f"({index}/{total}) {label}: falhou — {e}")
            else:
                await say(f"Nao consegui obter a imagem: {e}", final=True)
            continue

        caption = f"Camera: {label}"
        if total > 1:
            caption = f"{index}/{total} — {caption}"
        caption = caption[:1024]
        fname = f"shakira_{camera_id}.jpg"

        await pulse_whatsapp_typing()
        ok = await evo.send_image_bytes(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            image_bytes=image_bytes,
            filename=fname,
            caption=caption,
        )
        if ok is None:
            result.failed.append(camera_id)
            await say(f"Capturei {label} mas nao consegui enviar pelo WhatsApp.")
        else:
            result.sent += 1

        if index < total and _SNAPSHOT_DELAY_SEC > 0:
            await asyncio.sleep(_SNAPSHOT_DELAY_SEC)

    if result.sent == total:
        result.summary = f"{result.sent} imagem(ns) enviada(s)."
    elif result.sent:
        result.summary = (
            f"{result.sent} de {total} imagem(ns) enviada(s)."
            + (f" Falharam: {', '.join(result.failed)}." if result.failed else "")
        )
    else:
        result.summary = "Nao foi possivel enviar as imagens das cameras."

    if total > 1 and result.sent:
        await say(result.summary, final=True)

    return result


async def handle_camera_snapshot_decision(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    cameras: CamerasCatalog,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> CameraSnapshotsResult:
    """Interpreta decisao Gemini e envia snapshots."""
    intro = str(decision.get("response") or "").strip()
    camera_ids, error = parse_camera_snapshot_targets(decision, cameras)

    if error and not camera_ids:
        evo_base = settings.evolution_base_url.strip()
        evo_key = settings.evolution_api_key.strip()
        msg = (intro + "\n\n" + error).strip() if intro else error
        if messenger:
            await messenger.step(msg, final=True)
        elif evo_base and evo_key and instance:
            await evo.send_text(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                text=truncate_whatsapp(msg),
            )
        return CameraSnapshotsResult(summary=error)

    return await send_camera_snapshots(
        settings=settings,
        cameras=cameras,
        evo=evo,
        http=http,
        phone=phone,
        instance=instance,
        camera_ids=camera_ids,
        intro=intro,
        messenger=messenger,
    )
