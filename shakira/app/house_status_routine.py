"""Rotina: mosaico de cameras + analise visual + resumo da casa (chuva e alarme)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import google.generativeai as genai
import httpx

from app.alerts_catalog import AlertsCatalog, RainDispatchConfig
from app.amt_alarm_zones import ZONE_ENTITY_RE, zone_entities_from_catalog
from app.camera_snapshots import send_camera_snapshots
from app.camera_vision import CameraMosaicAnalysis, analyze_camera_mosaic
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.devices_catalog import DevicesCatalog, EntityConfig
from app.evolution import EvolutionClient
from app.homeassistant import HomeAssistantClient
from app.house_status_prompts import HOUSE_STATUS_SYSTEM, build_house_status_prompt
from app.user_friendly import entity_display_name, format_state_value
from app.whatsapp_steps import StepMessenger, pulse_whatsapp_typing, truncate_whatsapp

log = logging.getLogger(__name__)

_ALARM_PARTITION_RE = re.compile(r"^alarm_control_panel\.", re.IGNORECASE)
_ALARM_BINARY_RE = re.compile(r"^binary_sensor\.amt_8000_", re.IGNORECASE)
_ALARM_SENSOR_KINDS = frozenset({"contact", "infrared", "motion"})

_PARTITION_STATE_PT = {
    "disarmed": "desarmado",
    "armed_home": "armado (modo casa)",
    "armed_away": "armado (ausente)",
    "armed_night": "armado (noite)",
    "armed_vacation": "armado (férias)",
    "armed_custom_bypass": "armado (parcial)",
    "pending": "armando…",
    "triggered": "DISPARADO",
    "arming": "armando…",
}


def _is_alarm_entity(ent: EntityConfig) -> bool:
    eid = ent.entity_id.strip()
    if _ALARM_PARTITION_RE.match(eid):
        return True
    if ZONE_ENTITY_RE.match(eid):
        return True
    if _ALARM_BINARY_RE.match(eid):
        return True
    if ent.sensor_kind in _ALARM_SENSOR_KINDS:
        return True
    return False


def collect_house_status_entity_ids(
    catalog: DevicesCatalog,
    rain_config: RainDispatchConfig | None = None,
) -> list[str]:
    """Entidades de chuva e alarme para o resumo da casa."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(eid: str) -> None:
        eid = eid.strip()
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)

    for device in catalog.devices:
        for ent in device.entities:
            if ent.sensor_kind == "rain" or _is_alarm_entity(ent):
                add(ent.entity_id)

    for eid, _label in zone_entities_from_catalog(catalog):
        add(eid)

    if rain_config:
        add(rain_config.rain_entity)
        add(rain_config.volume_entity)

    return ordered


def humanize_zone_or_sensor_state(entity_id: str, raw_state: str) -> str:
    s = (raw_state or "").strip().lower()
    if not s or s in ("unknown", "unavailable"):
        return "indisponível"
    if _ALARM_PARTITION_RE.match(entity_id):
        return _PARTITION_STATE_PT.get(s, s.replace("_", " "))
    if s in ("open", "on", "triggered", "active", "violated", "motion", "detected"):
        return "aberto/ativado"
    if s in ("closed", "off", "inactive", "idle", "clear", "no_motion", "rest", "dry", "ok"):
        return "fechado/normal"
    return raw_state.strip()


def build_sensor_context_block(
    *,
    catalog: DevicesCatalog,
    states_by_id: dict[str, dict[str, Any]],
    rain_config: RainDispatchConfig | None = None,
) -> str:
    """Texto compacto dos sensores de chuva e alarme para o prompt Gemini."""
    rain_lines: list[str] = []
    alarm_lines: list[str] = []

    entity_ids = collect_house_status_entity_ids(catalog, rain_config)

    for eid in entity_ids:
        state = states_by_id.get(eid)
        if not state:
            continue
        ent = catalog.get_entity(eid)
        label = entity_display_name(eid, catalog, state)
        raw = str(state.get("state", "")).strip()

        is_rain = bool(ent and ent.sensor_kind == "rain")
        if rain_config and eid in (rain_config.rain_entity, rain_config.volume_entity):
            is_rain = True

        if is_rain:
            if rain_config and eid == rain_config.rain_entity:
                if raw.lower() == "on":
                    rain_lines.append("Está chovendo agora.")
                elif raw.lower() == "off":
                    rain_lines.append("Não está chovendo.")
                else:
                    rain_lines.append(humanize_zone_or_sensor_state(eid, raw).capitalize() + ".")
            elif rain_config and eid == rain_config.volume_entity:
                try:
                    mm = float(raw.replace(",", "."))
                    rain_lines.append(f"Volume de chuva (15 min): {mm:g} mm.")
                except ValueError:
                    rain_lines.append(f"Volume de chuva: {raw or 'indisponível'}.")
            else:
                rain_lines.append(format_state_value(eid, state, catalog))
            continue

        if _ALARM_PARTITION_RE.match(eid):
            alarm_lines.append(f"{label}: {humanize_zone_or_sensor_state(eid, raw)}.")
        elif ZONE_ENTITY_RE.match(eid) or (ent and _is_alarm_entity(ent)):
            alarm_lines.append(f"{label}: {humanize_zone_or_sensor_state(eid, raw)}.")

    sections: list[str] = []
    if rain_lines:
        sections.append("Chuva:\n" + "\n".join(f"- {line}" for line in rain_lines))
    if alarm_lines:
        sections.append("Alarme e sensores:\n" + "\n".join(f"- {line}" for line in alarm_lines))
    return "\n\n".join(sections).strip()


def generate_house_status_summary(
    *,
    api_key: str,
    vision_analysis: CameraMosaicAnalysis | None,
    sensor_context: str,
    model: str | None = None,
) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    prompt = build_house_status_prompt(
        vision_analysis=vision_analysis,
        sensor_context=sensor_context,
    )
    try:
        genai.configure(api_key=key)
        gemini = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=HOUSE_STATUS_SYSTEM,
        )
        response = gemini.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(temperature=0.35),
        )
    except Exception:
        log.exception("Gemini resumo house_status falhou")
        return ""

    text = getattr(response, "text", None) or ""
    if not text and response.candidates:
        parts = response.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts)
    return (text or "").strip()[:4000]


def _fallback_summary(
    vision_analysis: CameraMosaicAnalysis | None,
    sensor_context: str,
) -> str:
    from app.camera_vision import format_analysis_message

    parts: list[str] = ["Situação da casa:"]
    if vision_analysis:
        vision_text = format_analysis_message(vision_analysis)
        if vision_text:
            parts.append("")
            parts.append(vision_text)
    if sensor_context.strip():
        parts.append("")
        parts.append(sensor_context.strip())
    return "\n".join(parts).strip()[:4000]


async def handle_house_status(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    alerts_catalog: AlertsCatalog | None = None,
    messenger: StepMessenger | None = None,
) -> str:
    """Captura todas as cameras, analisa e envia resumo integrado ao usuario."""
    intro = str(decision.get("response") or "").strip()
    rain_config = alerts_catalog.rain_dispatch if alerts_catalog else None

    async def say(text: str, *, final: bool = False) -> None:
        msg = truncate_whatsapp(text)
        if not msg:
            return
        if messenger:
            await messenger.step(msg, final=final)
        else:
            evo_base = settings.evolution_base_url.strip()
            evo_key = settings.evolution_api_key.strip()
            if evo_base and evo_key and instance:
                await pulse_whatsapp_typing()
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    text=msg,
                )

    if intro:
        await say(intro)

    await say("Vou capturar todas as câmeras e analisar a situação da casa...")

    camera_ids, _err = cameras.resolve_camera_targets(all_cameras=True)
    snapshot_result = await send_camera_snapshots(
        settings=settings,
        cameras=cameras,
        evo=evo,
        http=http,
        phone=phone,
        instance=instance,
        camera_ids=camera_ids,
        intro="",
        messenger=None,
        send_progress=False,
        send_summary=False,
    )

    if snapshot_result.sent <= 0 or not snapshot_result.image_bytes:
        msg = snapshot_result.summary or "Não consegui capturar as câmeras agora."
        await say(msg, final=True)
        return msg

    api_key = settings.gemini_api_key.strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    vision_analysis: CameraMosaicAnalysis | None = None
    if api_key:
        try:
            vision_analysis = await asyncio.to_thread(
                analyze_camera_mosaic,
                api_key=api_key,
                image_bytes=snapshot_result.image_bytes,
                camera_panels=snapshot_result.image_panels,
                context="Pedido do morador: resumo da situação atual da casa.",
                model=model,
            )
        except Exception:
            log.exception("house_status: falha analise visual phone=%s", phone)

    entity_ids = collect_house_status_entity_ids(catalog, rain_config)
    states_by_id: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        state = await ha.get_state(eid)
        if state:
            states_by_id[eid] = state

    sensor_context = build_sensor_context_block(
        catalog=catalog,
        states_by_id=states_by_id,
        rain_config=rain_config,
    )

    summary = ""
    if api_key:
        summary = await asyncio.to_thread(
            generate_house_status_summary,
            api_key=api_key,
            vision_analysis=vision_analysis,
            sensor_context=sensor_context,
            model=model,
        )

    if not summary:
        summary = _fallback_summary(vision_analysis, sensor_context)

    if not summary:
        summary = "Capturei as câmeras, mas não consegui gerar o resumo da casa agora."

    await say(summary, final=True)
    log.info("house_status enviado phone=%s chars=%s", phone, len(summary))
    return summary
