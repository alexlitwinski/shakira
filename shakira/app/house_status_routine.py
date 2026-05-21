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
from app.alarm_dispatch_runner import resolve_camera_ids_for_partitions
from app.amt_alarm_zones import ZONE_ENTITY_RE, zone_entities_from_catalog
from app.camera_snapshots import send_camera_snapshots
from app.camera_vision import (
    CameraMosaicAnalysis,
    CameraPresence,
    HouseStatusMosaicInput,
    analyze_house_status_mosaics,
)
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

_EXTRA_PROBLEM_ENTITY_IDS = (
    "binary_sensor.status_cameras_paradas",
    "binary_sensor.amt_8000_falha_na_conexao",
)

# Mosaicos enviados no house_status: (rotulo WhatsApp, chave do grupo no shakira_cameras.yaml)
HOUSE_STATUS_MOSAICS: tuple[tuple[str, str], ...] = (
    ("Interna", "Interna"),
    ("Portão Social", "Portao Social"),
    ("Externas", "alarm_control_panel.amt_8000_partition_1"),
)

EXTERNAL_PARTITION_ENTITY = "alarm_control_panel.amt_8000_partition_1"


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


def resolve_house_status_camera_groups(
    cameras: CamerasCatalog,
) -> list[tuple[str, list[str]]]:
    """Resolve os 3 mosaicos (Interna, Portão Social, Externas) para house_status."""
    groups: list[tuple[str, list[str]]] = []
    for label, group_key in HOUSE_STATUS_MOSAICS:
        if group_key.startswith("alarm_control_panel."):
            ids = resolve_camera_ids_for_partitions(cameras, [group_key])
        else:
            resolved_group = cameras.resolve_group_name(group_key)
            if not resolved_group:
                ids = []
            else:
                ids = [cam.id for cam in cameras.cameras_for_group(resolved_group)]
        if ids:
            groups.append((label, ids))
    return groups


def merge_vision_analyses(
    sections: list[tuple[str, CameraMosaicAnalysis]],
) -> CameraMosaicAnalysis | None:
    """Combina analises visuais de varios mosaicos num unico objeto para o resumo."""
    if not sections:
        return None
    all_cameras: list[CameraPresence] = []
    descriptions: list[str] = []
    recommendations: list[str] = []
    for label, analysis in sections:
        all_cameras.extend(analysis.cameras)
        if analysis.description.strip():
            descriptions.append(f"{label}:\n{analysis.description.strip()}")
        if analysis.recommendation.strip():
            recommendations.append(f"{label}: {analysis.recommendation.strip()}")
    return CameraMosaicAnalysis(
        cameras=all_cameras,
        description="\n\n".join(descriptions),
        recommendation=" ".join(recommendations),
    )


def collect_problem_entity_ids(catalog: DevicesCatalog) -> list[str]:
    """Entidades do catalogo (+ extras conhecidos) para detectar indisponibilidade."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(eid: str) -> None:
        eid = eid.strip()
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)

    for eid in catalog.context_entity_ids():
        add(eid)
    for eid in _EXTRA_PROBLEM_ENTITY_IDS:
        add(eid)
    return ordered


def describe_entity_problem(
    entity_id: str,
    state: dict[str, Any] | None,
    *,
    catalog: DevicesCatalog,
) -> str | None:
    """Retorna descricao curta do problema ou None se a entidade estiver OK."""
    label = entity_display_name(entity_id, catalog, state)

    if state is None:
        return f"{label}: sem resposta do Home Assistant"

    raw = str(state.get("state", "")).strip()
    low = raw.lower()
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

    if entity_id == "binary_sensor.status_cameras_paradas" and low == "on":
        return f"{label}: câmeras com problema"

    if entity_id == "binary_sensor.amt_8000_falha_na_conexao" and low == "on":
        return f"{label}: falha na conexão"

    if domain == "binary_sensor" and "ping" in entity_id.lower() and low == "off":
        return f"{label}: offline"

    if low == "unavailable":
        return f"{label}: indisponível"
    if low in ("unknown", ""):
        return f"{label}: estado desconhecido"

    return None


def build_problem_devices_block(
    *,
    catalog: DevicesCatalog,
    states_by_id: dict[str, dict[str, Any]],
) -> str:
    """Lista compacta de dispositivos com problema para o prompt Gemini."""
    problems: list[str] = []
    for eid in collect_problem_entity_ids(catalog):
        issue = describe_entity_problem(eid, states_by_id.get(eid), catalog=catalog)
        if issue:
            problems.append(f"- {issue}")
    if not problems:
        return ""
    return "Dispositivos com problema:\n" + "\n".join(problems)


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
    vision_sections: list[tuple[str, CameraMosaicAnalysis]] | None = None,
    vision_analysis: CameraMosaicAnalysis | None = None,
    sensor_context: str,
    problems_context: str = "",
    model: str | None = None,
) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    model_name = (model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")).strip()
    sections = vision_sections or []
    if not sections and vision_analysis:
        sections = [("Câmeras", vision_analysis)]
    prompt = build_house_status_prompt(
        vision_sections=sections,
        sensor_context=sensor_context,
        problems_context=problems_context,
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
    vision_sections: list[tuple[str, CameraMosaicAnalysis]] | None = None,
    vision_analysis: CameraMosaicAnalysis | None = None,
    sensor_context: str = "",
    problems_context: str = "",
) -> str:
    from app.camera_vision import format_analysis_message

    parts: list[str] = ["Situação da casa:"]
    sections = vision_sections or []
    if not sections and vision_analysis:
        sections = [("Câmeras", vision_analysis)]
    for label, analysis in sections:
        vision_text = format_analysis_message(analysis)
        if vision_text:
            parts.append("")
            parts.append(f"{label}:")
            parts.append(vision_text)
    if sensor_context.strip():
        parts.append("")
        parts.append(sensor_context.strip())
    if problems_context.strip():
        parts.append("")
        parts.append(problems_context.strip())
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
    catalog_states_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Captura todas as cameras, analisa e envia resumo integrado ao usuario."""
    intro = str(decision.get("response") or "").strip()
    rain_config = alerts_catalog.rain_dispatch if alerts_catalog else None
    states_map = catalog_states_map or {}

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

    mosaic_groups = resolve_house_status_camera_groups(cameras)
    if not mosaic_groups:
        msg = "Nenhuma câmera configurada para o resumo da casa."
        await say(msg, final=True)
        return msg

    await say(
        "Vou capturar as câmeras por área (Interna, Portão Social e Externas) "
        "e analisar a situação da casa..."
    )

    api_key = settings.gemini_api_key.strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    captured_mosaics: list[HouseStatusMosaicInput] = []
    mosaics_sent = 0

    for area_label, camera_ids in mosaic_groups:
        snapshot_result = await send_camera_snapshots(
            settings=settings,
            cameras=cameras,
            evo=evo,
            http=http,
            phone=phone,
            instance=instance,
            camera_ids=camera_ids,
            intro="",
            area_label=area_label,
            messenger=None,
            send_progress=False,
            send_summary=False,
            quiet_send_failure=True,
        )

        if snapshot_result.sent > 0:
            mosaics_sent += 1

        if not snapshot_result.image_bytes:
            log.warning(
                "house_status: sem imagem para area=%s phone=%s",
                area_label,
                phone,
            )
            continue

        captured_mosaics.append(
            HouseStatusMosaicInput(
                area_label=area_label,
                image_bytes=snapshot_result.image_bytes,
                camera_panels=tuple(snapshot_result.image_panels),
            )
        )

    if not captured_mosaics:
        msg = "Não consegui capturar as câmeras agora."
        await say(msg, final=True)
        return msg

    vision_sections: list[tuple[str, CameraMosaicAnalysis]] = []
    if api_key:
        try:
            vision_sections = await asyncio.to_thread(
                analyze_house_status_mosaics,
                api_key=api_key,
                mosaics=captured_mosaics,
                context="Pedido do morador: resumo da situação atual da casa.",
                model=model,
            )
        except Exception:
            log.exception("house_status: falha analise visual phone=%s", phone)

    if not vision_sections:
        log.warning(
            "house_status: Gemini Vision nao analisou mosaicos phone=%s capturados=%s",
            phone,
            len(captured_mosaics),
        )

    entity_ids = collect_house_status_entity_ids(catalog, rain_config)
    states_by_id: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        cached = states_map.get(eid)
        if cached:
            states_by_id[eid] = cached
            continue
        state = await ha.get_state(eid)
        if state:
            states_by_id[eid] = state

    for eid in collect_problem_entity_ids(catalog):
        if eid in states_by_id:
            continue
        cached = states_map.get(eid)
        if cached:
            states_by_id[eid] = cached
            continue
        state = await ha.get_state(eid)
        if state:
            states_by_id[eid] = state

    sensor_context = build_sensor_context_block(
        catalog=catalog,
        states_by_id=states_by_id,
        rain_config=rain_config,
    )
    problems_context = build_problem_devices_block(
        catalog=catalog,
        states_by_id=states_by_id,
    )

    summary = ""
    if api_key:
        summary = await asyncio.to_thread(
            generate_house_status_summary,
            api_key=api_key,
            vision_sections=vision_sections,
            sensor_context=sensor_context,
            problems_context=problems_context,
            model=model,
        )

    if not summary:
        summary = _fallback_summary(
            vision_sections=vision_sections,
            sensor_context=sensor_context,
            problems_context=problems_context,
        )

    if not summary:
        summary = "Capturei as câmeras, mas não consegui gerar o resumo da casa agora."

    await say(summary, final=True)
    log.info("house_status enviado phone=%s chars=%s", phone, len(summary))
    return summary
