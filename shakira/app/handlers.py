"""Processamento de webhooks Evolution e fluxo Gemini + Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.camera_snapshots import handle_camera_snapshot_decision
from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.confirmation_context import (
    augment_user_message_for_affirmative,
    confirmation_execution_retry_message,
    correct_affirmative_misroute,
    last_assistant_text,
    needs_confirmation_execution_retry,
)
from app.conversation_history import format_for_prompt, get_recent, record_exchange
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.gemini import GeminiAssistant
from app.gemini_cache import ensure_catalog_cache
from app.ha_states_cache import (
    filter_states_for_ids,
    get_all_states_cached,
    get_states_map_cached,
    store_all_states,
)
from app.homeassistant import HomeAssistantClient
from app.message_timing import MessageTimings
from app.photoprism import (
    PhotoprismAuthError,
    PhotoprismClient,
    PhotoprismError,
    PhotoResult,
    build_search_attempts,
    expand_city_variants,
    format_upload_user_message,
    normalize_photo_filters,
    photo_matches_place,
)
from app.scenario_context import (
    build_friendly_reply_from_scenario,
    build_gemini_scenario_correction_block,
    fetch_verified_entity_states,
    prepend_scenario_states_to_context,
)
from app.scheduled_responses import (
    MIN_ACTION_DELAY_SECONDS,
    ensure_runner_started,
    format_pending_for_prompt,
    get_scheduled_store,
)
from app.state_conditions import state_matches
from app.user_memory import InboundContent, InboundMedia, UserMemoryStore, get_store
from app.whatsapp_phones import (
    ENTITY_PERMITTED,
    fetch_permitted_phones_raw,
    normalize_phone_digits,
    parse_allowed_numbers,
)

from app.user_memory_cache import ensure_user_memory_cache, invalidate_user_memory_cache
from app.user_memory_prompts import USER_MEMORY_ACTIONS_INSTRUCTION
from app.instagram_links_prompts import INSTAGRAM_LINKS_ACTIONS_INSTRUCTION
from app.fact_check_prompts import FACT_CHECK_ACTIONS_INSTRUCTION
from app.fact_check_actions import handle_fact_check_claim
from app.fact_check_overrides import try_fact_check_decision_override
from app.google_calendar_prompts import GOOGLE_CALENDAR_ACTIONS_INSTRUCTION
from app.google_calendar_actions import (
    handle_google_calendar_configure,
    handle_google_calendar_list_events,
    handle_google_calendar_save_link,
    handle_google_calendar_show_settings,
)
from app.google_calendar_store import get_google_calendar_store
from app.google_calendar_runner import ensure_google_calendar_runner_running
from app.google_calendar_routine import try_handle_google_calendar_link_inbound
from app.google_calendar_overrides import try_google_calendar_decision_override
from app.birthday_prompts import BIRTHDAY_ACTIONS_INSTRUCTION
from app.birthday_actions import (
    execute_birthday_save_batch,
    handle_birthday_delete,
    handle_birthday_list,
    handle_birthday_save,
    handle_birthday_upcoming,
)
from app.birthday_store import get_birthday_store
from app.birthday_runner import ensure_birthday_runner_running
from app.user_memory_actions import (
    handle_delete_from_memory,
    try_memory_delete_override,
    try_personal_registry_list_reply,
)
from app.instagram_links_actions import (
    handle_delete_instagram_link,
    handle_search_instagram_links,
    resolve_entry_for_reference,
    try_handle_refresh_instagram_inbound,
    try_instagram_links_list_reply,
    try_search_instagram_profiles_reply,
)
from app.instagram_links_routine import (
    is_instagram_link_pending,
    try_handle_instagram_link_inbound,
    try_handle_instagram_link_pending,
)
from app.instagram_links_store import get_instagram_store
from app.instagram_profile_fetcher import enrich_and_notify_instagram_profile, format_profile_summary
from app.password_vault_routine import (
    handle_vault_gemini_decision,
    try_handle_password_vault_pending,
    try_handle_vault_intent_direct,
)
from app.portao_social_routine import (
    try_handle_portao_servico_inbound,
    try_handle_portao_social_inbound,
)
from app.audio_transcription import resolve_inbound_audio_as_text
from app.pending_media import (
    is_inbound_audio,
    is_storable_file_media,
    build_media_choice_prompt,
    build_pending_clarification,
    build_pending_collecting_wait,
    build_pending_processing_wait,
    build_pending_progress_message,
    build_personal_description_prompt,
    cancel_media_batch_notification,
    classify_explicit_media_intent,
    classify_pending_reply,
    download_inbound_media_bytes,
    extract_album_name,
    extract_personal_description,
    is_gallery_media,
    is_placeholder_user_text,
    media_has_explicit_intent,
    pending_gallery_stats,
    _enqueue_media_batch_notification,
    _media_batch_lock,
)
from app.user_friendly import (
    format_action_in_progress,
    format_action_success,
    format_checking,
    format_ha_error_user,
    format_state_value,
    is_internal_instruction_leak,
    polish_user_message,
)
from app.whatsapp_steps import (
    StepMessenger,
    TypingSession,
    pulse_whatsapp_typing,
    truncate_whatsapp,
)

log = logging.getLogger(__name__)

BOILER_MODE_ENTITY = "input_select.modo_do_boiler"
BOILER_TEMP_ENTITY = "sensor.temperatura_boiler"
BOILER_READY_WHEN = ">=42"
BOILER_SCHEDULE_LABEL = "aviso boiler banho"

VALID_GEMINI_ACTIONS = frozenset(
    {
        "reply",
        "call_service",
        "get_state",
        "list_entities",
        "search_photos",
        "get_camera_snapshot",
        "save_memory",
        "send_user_file",
        "delete_from_memory",
        "vault_save",
        "vault_retrieve",
        "vault_list",
        "schedule_response",
        "schedule_action",
        "cancel_scheduled_response",
        "list_instagram_links",
        "search_instagram_links",
        "refresh_instagram_link",
        "delete_instagram_link",
        "send_instagram_link",
        "fact_check_claim",
        "google_calendar_save_link",
        "google_calendar_configure",
        "google_calendar_list_events",
        "google_calendar_show_settings",
        "birthday_save",
        "birthday_list",
        "birthday_delete",
        "birthday_upcoming",
    }
)

# Acoes cujo handler ja devolve a mensagem final — nao concatenar reply + result.
_SINGLE_USER_MESSAGE_ACTIONS = frozenset(
    {
        "schedule_response",
        "schedule_action",
        "cancel_scheduled_response",
        "save_memory",
        "delete_from_memory",
        "vault_save",
        "vault_retrieve",
        "vault_list",
        "list_instagram_links",
        "search_instagram_links",
        "refresh_instagram_link",
        "delete_instagram_link",
        "fact_check_claim",
        "google_calendar_save_link",
        "google_calendar_configure",
        "google_calendar_list_events",
        "google_calendar_show_settings",
        "birthday_save",
        "birthday_list",
        "birthday_delete",
        "birthday_upcoming",
    }
)

_USER_MEMORY_CACHE_MIN_CHARS = int(os.environ.get("SHAKIRA_USER_MEMORY_CACHE_MIN_CHARS", "6000"))
_GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "1"))

_PLACEHOLDER_RESPONSE_MARKERS = (
    "verificando",
    "aguarde",
    "um momento",
    "vou verificar",
    "deixa eu ver",
)

_pending_unlock: dict[str, "PendingUnlock"] = {}

# Evita processar o mesmo texto duas vezes (webhook duplo / eco).
_inbound_dedup: dict[str, float] = {}
_inbound_dedup_lock = threading.Lock()
_INBOUND_DEDUP_SEC = float(os.environ.get("INBOUND_DEDUP_SEC", "5"))

_IGNORE_MESSAGE_TYPES = frozenset(
    {
        "protocolMessage",
        "reactionMessage",
        "senderKeyDistributionMessage",
        "pollUpdateMessage",
        "ephemeralMessage",
    }
)


@dataclass
class PendingUnlock:
    entity_id: str
    domain: str
    service: str
    service_data: dict[str, Any]


def normalize_evolution_payload(payload: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    root_instance = payload.get("instance") or payload.get("instanceName")
    records: list[tuple[str | None, dict[str, Any]]] = []

    def push(inst: str | None, item: dict[str, Any]) -> None:
        records.append((inst or root_instance, item))

    data = payload.get("data")
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                push(root_instance, row)
        return records

    if isinstance(data, dict):
        inner = data.get("messages") if isinstance(data.get("messages"), list) else None
        if inner:
            for row in inner:
                if isinstance(row, dict):
                    push(root_instance, row)
            return records
        push(root_instance, data)
        return records

    if payload.get("key") or payload.get("message"):
        push(root_instance, payload)

    return records


def _is_outbound_evolution_message(record: dict[str, Any], key: dict[str, Any]) -> bool:
    return key.get("fromMe") is True or record.get("fromMe") is True


def _is_echo_of_last_assistant(phone: str, text: str) -> bool:
    entries = get_recent(phone)
    if not entries:
        return False
    last = entries[-1]
    if last.role != "assistant":
        return False
    return last.text.strip() == text.strip()


def _normalize_action_name(raw: Any) -> str:
    return str(raw or "reply").strip().lower()


def _inbound_dedup_key(phone: str, text: str, record: dict[str, Any] | None = None) -> str:
    """Chave unica por mensagem; midias sem legenda compartilham o mesmo texto placeholder."""
    if isinstance(record, dict):
        key = record.get("key") or {}
        if isinstance(key, dict):
            msg_id = key.get("id") or record.get("messageId") or record.get("id")
            if msg_id:
                return f"{phone}:msg:{msg_id}"
    return f"{phone}:{text}"


def _accept_inbound_once(phone: str, text: str, record: dict[str, Any] | None = None) -> bool:
    key = _inbound_dedup_key(phone, text, record)
    now = time.monotonic()
    with _inbound_dedup_lock:
        last = _inbound_dedup.get(key)
        if last is not None and now - last < _INBOUND_DEDUP_SEC:
            return False
        _inbound_dedup[key] = now
        if len(_inbound_dedup) > 500:
            cutoff = now - _INBOUND_DEDUP_SEC * 2
            for k, ts in list(_inbound_dedup.items()):
                if ts < cutoff:
                    del _inbound_dedup[k]
    return True


def _unwrap_whatsapp_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Desembrulha ephemeral/viewOnce/documentWithCaption etc."""
    current = msg
    for _ in range(6):
        if not isinstance(current, dict):
            break
        nested: dict[str, Any] | None = None
        for key in (
            "ephemeralMessage",
            "viewOnceMessage",
            "viewOnceMessageV2",
            "documentWithCaptionMessage",
        ):
            wrap = current.get(key)
            if isinstance(wrap, dict):
                inner = wrap.get("message")
                nested = inner if isinstance(inner, dict) else wrap
                break
        if nested is None:
            break
        current = nested
    return current if isinstance(current, dict) else msg


def _media_from_part(part: dict[str, Any], mediatype: str) -> InboundMedia:
    defaults = {
        "image": ("imagem.jpg", "image/jpeg"),
        "document": ("documento", "application/octet-stream"),
        "video": ("video.mp4", "video/mp4"),
        "audio": ("audio.ogg", "audio/ogg"),
    }
    default_name, default_mime = defaults.get(mediatype, ("arquivo", "application/octet-stream"))
    return InboundMedia(
        mediatype=mediatype,
        filename=str(part.get("fileName") or part.get("title") or default_name),
        mimetype=str(part.get("mimetype") or part.get("mimeType") or default_mime),
        caption=str(part.get("caption") or "").strip(),
        message_record={},
    )


def _extract_media_from_message(msg: dict[str, Any]) -> InboundMedia | None:
    body = _unwrap_whatsapp_message(msg)
    if isinstance(body.get("imageMessage"), dict):
        return _media_from_part(body["imageMessage"], "image")
    if isinstance(body.get("documentMessage"), dict):
        return _media_from_part(body["documentMessage"], "document")
    if isinstance(body.get("videoMessage"), dict):
        return _media_from_part(body["videoMessage"], "video")
    if isinstance(body.get("audioMessage"), dict):
        return _media_from_part(body["audioMessage"], "audio")
    if isinstance(body.get("stickerMessage"), dict):
        return _media_from_part(body["stickerMessage"], "image")
    return None


def extract_inbound_content(record: dict[str, Any]) -> InboundContent | None:
    """Extrai telefone, texto e opcionalmente midia de um registro Evolution."""
    key = record.get("key") or {}
    if not isinstance(key, dict):
        key = {}

    msg_type = str(record.get("messageType") or "").strip()
    if msg_type in _IGNORE_MESSAGE_TYPES:
        log.debug("Ignorando messageType=%s", msg_type)
        return None

    remote = key.get("remoteJid") or record.get("remoteJid") or ""
    if not remote:
        return None
    if remote.endswith("@g.us"):
        log.debug("Ignorando grupo: %s", remote)
        return None

    if _is_outbound_evolution_message(record, key):
        log.debug("Ignorando mensagem enviada pelo bot (fromMe/status)")
        return None

    digits = normalize_phone_digits(remote.split("@")[0])
    if not digits:
        return None

    raw_msg = record.get("message") or {}
    msg = _unwrap_whatsapp_message(raw_msg) if isinstance(raw_msg, dict) else {}

    text = ""
    if isinstance(msg.get("conversation"), str):
        text = msg["conversation"]
    elif isinstance(msg.get("extendedTextMessage"), dict):
        text = msg["extendedTextMessage"].get("text") or ""
    elif isinstance(msg.get("buttonsResponseMessage"), dict):
        text = msg["buttonsResponseMessage"].get("selectedDisplayText") or ""
    elif isinstance(msg.get("listResponseMessage"), dict):
        t = msg["listResponseMessage"].get("title")
        d = msg["listResponseMessage"].get("description")
        text = f"{t or ''} {d or ''}".strip()

    media = _extract_media_from_message(msg if isinstance(msg, dict) else {})
    if not media:
        msg_type = str(record.get("messageType") or "").strip()
        if msg_type in (
            "documentMessage",
            "imageMessage",
            "videoMessage",
            "audioMessage",
            "stickerMessage",
            "documentWithCaptionMessage",
        ):
            log.warning(
                "messageType=%s mas midia nao extraida; chaves message=%s",
                msg_type,
                list(msg.keys()) if isinstance(msg, dict) else type(msg),
            )
    if media:
        media.message_record = record
        if not text.strip() and media.caption:
            text = media.caption

    text = text.strip()
    if not text and not media:
        return None

    if text and _is_echo_of_last_assistant(digits, text):
        log.info("Ignorando eco da ultima resposta do assistente phone=%s", digits)
        return None

    if not _accept_inbound_once(digits, text or "[media]", record):
        log.info("Ignorando mensagem duplicada phone=%s", digits)
        return None

    if not text and media:
        if is_inbound_audio(media.mediatype, media.mimetype):
            text = ""
        else:
            text = "[usuario enviou um arquivo]"

    return InboundContent(phone=digits, text=text, media=media, record=record)


def extract_text_and_sender(record: dict[str, Any]) -> tuple[str | None, str | None]:
    key = record.get("key") or {}
    if not isinstance(key, dict):
        key = {}

    msg_type = str(record.get("messageType") or "").strip()
    if msg_type in _IGNORE_MESSAGE_TYPES:
        log.debug("Ignorando messageType=%s", msg_type)
        return None, None

    remote = key.get("remoteJid") or record.get("remoteJid") or ""
    if not remote:
        return None, None
    if remote.endswith("@g.us"):
        log.debug("Ignorando grupo: %s", remote)
        return None, None

    if _is_outbound_evolution_message(record, key):
        log.debug("Ignorando mensagem enviada pelo bot (fromMe/status)")
        return None, None

    digits = normalize_phone_digits(remote.split("@")[0])

    msg = record.get("message") or {}
    if isinstance(msg.get("ephemeralMessage"), dict):
        msg = msg["ephemeralMessage"].get("message") or msg

    text = ""
    if isinstance(msg.get("conversation"), str):
        text = msg["conversation"]
    elif isinstance(msg.get("extendedTextMessage"), dict):
        text = msg["extendedTextMessage"].get("text") or ""
    elif isinstance(msg.get("buttonsResponseMessage"), dict):
        text = msg["buttonsResponseMessage"].get("selectedDisplayText") or ""
    elif isinstance(msg.get("listResponseMessage"), dict):
        t = msg["listResponseMessage"].get("title")
        d = msg["listResponseMessage"].get("description")
        text = f"{t or ''} {d or ''}".strip()

    text = text.strip()
    if not digits or not text:
        return None, None

    if _is_echo_of_last_assistant(digits, text):
        log.info("Ignorando eco da ultima resposta do assistente phone=%s", digits)
        return None, None

    if not _accept_inbound_once(digits, text):
        log.info("Ignorando mensagem duplicada phone=%s", digits)
        return None, None

    return digits, text


def _log_message_timings(timings: MessageTimings, messenger: StepMessenger | None) -> None:
    wa_steps = len(messenger._parts) if messenger else 0
    timings.finish(wa_steps=wa_steps)
    log.info(
        "timing phone=%s ha_states_ms=%.0f gemini_ms=%.0f gemini_calls=%s wa_steps=%s total_ms=%.0f",
        timings.phone,
        timings.ha_states_ms,
        timings.gemini_ms,
        timings.gemini_calls,
        timings.wa_steps,
        timings.total_ms,
    )
    if log.isEnabledFor(logging.DEBUG):
        overhead_ms = max(
            0.0,
            timings.total_ms - timings.ha_states_ms - timings.gemini_ms,
        )
        log.debug(
            "timing detail phone=%s overhead_ms=%.0f gemini_avg_ms=%.0f",
            timings.phone,
            overhead_ms,
            timings.gemini_ms / timings.gemini_calls if timings.gemini_calls else 0.0,
        )


async def fetch_catalog_entity_states(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Estados HA das entidades do catalogo (1x get_states + filtro, com cache TTL)."""
    entity_ids = catalog.context_entity_ids()
    if not entity_ids:
        return [], {}

    cached_map = get_states_map_cached()
    if cached_map is not None:
        states = [cached_map[eid] for eid in entity_ids if eid in cached_map]
        by_id = {eid: cached_map[eid] for eid in entity_ids if eid in cached_map}
        log.debug(
            "fetch_catalog_entity_states cache map: %s/%s entidades",
            len(states),
            len(entity_ids),
        )
        return states, by_id

    cached_all = get_all_states_cached()
    if cached_all is not None:
        states = filter_states_for_ids(cached_all, entity_ids)
        by_id = {str(s.get("entity_id")): s for s in states if s.get("entity_id")}
        log.debug(
            "fetch_catalog_entity_states cache lista: %s/%s entidades",
            len(states),
            len(entity_ids),
        )
        return states, by_id

    all_states = await ha.get_states()
    store_all_states(all_states)
    states = filter_states_for_ids(all_states, entity_ids)
    by_id = {str(s.get("entity_id")): s for s in states if s.get("entity_id")}
    log.debug(
        "fetch_catalog_entity_states HA fresh: %s/%s entidades (total HA=%s)",
        len(states),
        len(entity_ids),
        len(all_states),
    )
    return states, by_id


def build_entities_context(states: list[dict[str, Any]]) -> tuple[str, int]:
    max_chars = int(os.environ.get("ENTITY_CONTEXT_MAX_CHARS", "40000"))
    total = len(states)
    if total == 0:
        return (
            "(Nenhuma entidade configurada no catalogo shakira_devices.)\n\n"
            "Total de entidades: 0",
            0,
        )
    lines: list[str] = []
    for s in sorted(states, key=lambda x: x.get("entity_id", "")):
        eid = s.get("entity_id", "")
        st = str(s.get("state", ""))
        name = ""
        extra = ""
        attrs = s.get("attributes") or {}
        if isinstance(attrs, dict):
            name = str(attrs.get("friendly_name") or "")
            domain = eid.split(".", 1)[0] if "." in eid else ""
            if domain == "climate":
                bits: list[str] = []
                if attrs.get("current_temperature") is not None:
                    bits.append(f"atual={attrs['current_temperature']}")
                if attrs.get("temperature") is not None:
                    bits.append(f"alvo={attrs['temperature']}")
                if bits:
                    extra = "\t" + " ".join(bits)
            elif domain == "sensor":
                unit = attrs.get("unit_of_measurement")
                if unit:
                    extra = f"\t{unit}"
        lines.append(f"{eid}\t{st}\t{name}{extra}")
    body = "\n".join(lines)
    if len(body) <= max_chars:
        return body + f"\n\nTotal de entidades: {total}", total
    truncated = body[:max_chars].rsplit("\n", 1)[0]
    note = f"\n\n[Contexto truncado] Mostrando apenas parte das {total} entidades."
    return truncated + note, total


def _truncate_whatsapp(text: str, limit: int = 3800) -> str:
    return truncate_whatsapp(text, limit)


async def _finish_whatsapp_exchange(
    *,
    phone: str,
    user_text: str,
    messenger: StepMessenger | None,
    reply_text: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> None:
    """Grava historico e envia resposta final se os passos ainda nao foram enviados."""
    final = polish_user_message(reply_text).strip()
    if messenger and messenger.sent_any:
        if final and final not in messenger.combined():
            await messenger.step(final, final=True)
            record_exchange(phone, user_text, messenger.combined())
        else:
            record_exchange(phone, user_text, messenger.combined())
        return
    text = _truncate_whatsapp(polish_user_message(reply_text))
    if not text:
        return
    if not evo_base or not evo_key or not instance:
        log.error("Envio Evolution bloqueado no fechamento da conversa")
        return
    await pulse_whatsapp_typing()
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=text,
    )
    record_exchange(phone, user_text, text)


def _resolve_evolution_instance(
    inst_hint: str | None,
    webhook_instance: Any,
    default_inst: str,
    settings: AppSettings,
) -> str:
    hint = inst_hint if isinstance(inst_hint, str) and inst_hint.strip() else ""
    return (hint or str(webhook_instance) or default_inst or settings.evolution_instance).strip()


def extract_target_entity_ids(
    domain: str,
    service: str,
    service_data: dict[str, Any] | None,
    decision_entity_id: Any = None,
) -> list[str]:
    if isinstance(decision_entity_id, str) and decision_entity_id.strip():
        return [decision_entity_id.strip()]
    if not service_data:
        return []
    eid = service_data.get("entity_id")
    if isinstance(eid, str) and eid.strip():
        return [eid.strip()]
    if isinstance(eid, list):
        return [str(x).strip() for x in eid if str(x).strip()]
    return []


def extract_target_entity_id(
    domain: str,
    service: str,
    service_data: dict[str, Any] | None,
    decision_entity_id: Any = None,
) -> str | None:
    ids = extract_target_entity_ids(domain, service, service_data, decision_entity_id)
    return ids[0] if ids else None


def _log_service_payload(label: str, payload: dict[str, Any]) -> None:
    safe = dict(payload)
    if "code" in safe:
        safe["code"] = "***"
    log.info("%s payload=%s", label, safe)


def _password_prompt_message(reply: str, prompt: str) -> str:
    """Combina texto do Gemini com o prompt do catalogo sem repetir a mesma pergunta."""
    reply = reply.strip()
    prompt = prompt.strip()
    if not prompt:
        return reply
    if not reply:
        return prompt
    norm_reply = " ".join(reply.lower().split())
    norm_prompt = " ".join(prompt.lower().split())
    if norm_reply == norm_prompt or norm_prompt in norm_reply:
        return reply
    if norm_reply in norm_prompt:
        return prompt
    return f"{reply}\n\n{prompt}"


def _is_placeholder_scenario_response(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _PLACEHOLDER_RESPONSE_MARKERS)


def _substantive_reply(reply: str) -> bool:
    return bool(reply.strip()) and not _is_placeholder_scenario_response(reply)


def _messages_are_redundant(a: str, b: str) -> bool:
    """True se duas mensagens dizem essencialmente a mesma coisa."""
    na = " ".join(a.lower().split())
    nb = " ".join(b.lower().split())
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na


def split_gemini_decisions(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Expande decisao com action=_batch ou chave batch/actions (varias acoes numa mensagem)."""
    if _normalize_action_name(decision.get("action")) == "_batch":
        batch = decision.get("batch")
        if isinstance(batch, list):
            out = [row for row in batch if isinstance(row, dict) and row.get("action")]
            if out:
                return out

    for key in ("batch", "actions", "steps", "decisions"):
        nested = decision.get(key)
        if isinstance(nested, list):
            out = [row for row in nested if isinstance(row, dict) and row.get("action")]
            if len(out) > 1:
                return out

    return [decision]


def normalize_gemini_action(
    decision: dict[str, Any], catalog: DevicesCatalog
) -> tuple[dict[str, Any], str | None]:
    """Corrige action invalida (ex.: id de cenario). Retorna (decision, scenario_id para retry)."""
    action = _normalize_action_name(decision.get("action"))
    if action == "_batch":
        return decision, None
    if action in VALID_GEMINI_ACTIONS:
        if action != str(decision.get("action") or "reply").strip().lower():
            fixed = dict(decision)
            fixed["action"] = action
            return fixed, None
        return decision, None

    scenario_ids = {s.id for s in catalog.scenarios}
    if action in scenario_ids:
        log.warning(
            "Gemini usou id de cenario como action='%s'; esperado reply/get_state/call_service",
            action,
        )
        fixed = dict(decision)
        fixed["action"] = "reply"
        if _is_placeholder_scenario_response(str(decision.get("response") or "")):
            fixed["response"] = ""
        return fixed, action

    log.warning("Acao Gemini desconhecida '%s'; convertendo para reply", action)
    fixed = dict(decision)
    fixed["action"] = "reply"
    return fixed, None


async def _apply_scenario_context_for_retry(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    ctx: str,
    scenario_id: str,
    *,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    ctx = await prepend_scenario_states_to_context(
        ha, catalog, ctx, scenario_id, states_map=states_map
    )
    correction = build_gemini_scenario_correction_block(scenario_id)
    return f"{correction}\n\n{ctx}"


async def _ensure_user_friendly_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    scenario_id: str | None,
    states_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Garante resposta amigavel; nunca envia instrucao interna ou dump tecnico."""
    if _normalize_action_name(decision.get("action")) == "_batch":
        return decision
    if len(split_gemini_decisions(decision)) > 1:
        return decision

    raw = str(decision.get("response") or "").strip()
    action = str(decision.get("action") or "reply").lower()
    if action not in VALID_GEMINI_ACTIONS:
        action = "reply"

    cleaned = polish_user_message(raw)
    needs_friendly = (
        not cleaned
        or is_internal_instruction_leak(raw)
        or _is_placeholder_scenario_response(raw)
    )

    if needs_friendly and scenario_id:
        friendly = await build_friendly_reply_from_scenario(
            ha, catalog, scenario_id, states_map=states_map
        )
        if friendly:
            log.info("Resposta amigavel HA cenario=%s (Gemini nao concluiu)", scenario_id)
            out = dict(decision)
            out["action"] = "reply"
            out["response"] = friendly
            return out

    if cleaned:
        out = dict(decision)
        out["action"] = action if action in VALID_GEMINI_ACTIONS else "reply"
        out["response"] = cleaned
        return out

    if raw and is_internal_instruction_leak(raw):
        log.warning("Bloqueada resposta com instrucao interna vazada")

    out = dict(decision)
    out["action"] = "reply"
    out["response"] = "Não consegui concluir a resposta agora. Tente de novo em instantes."
    return out


def _log_gemini_decision(phone: str, decision: dict[str, Any]) -> None:
    safe = dict(decision)
    if safe.get("provided_password"):
        safe["provided_password"] = "***"
    try:
        text = json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(safe)
    log.info("Gemini decisao phone=%s: %s", phone, text[:1000])


def build_ha_service_data(
    domain: str,
    service: str,
    entity_id: str,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Monta payload aceito pela API REST do HA (evita campos extras do JSON do Gemini)."""
    raw = raw or {}
    raw_eid = raw.get("entity_id")
    if isinstance(raw_eid, list) and raw_eid:
        data: dict[str, Any] = {
            "entity_id": [str(x).strip() for x in raw_eid if str(x).strip()]
        }
    else:
        data = {"entity_id": entity_id}
    # PIN da integracao lock no HA (diferente da senha Shakira no catalogo)
    if domain == "lock" and service in ("unlock", "lock", "open"):
        code = raw.get("code")
        if isinstance(code, str) and code.strip():
            data["code"] = code.strip()
    if domain == "input_select" and service == "select_option":
        option = raw.get("option")
        if isinstance(option, str) and option.strip():
            data["option"] = option.strip()
    if domain == "alarm_control_panel":
        code = raw.get("code")
        if isinstance(code, str) and code.strip():
            data["code"] = code.strip()
    if domain == "light":
        for key in ("brightness", "brightness_pct", "color_temp", "kelvin", "rgb_color", "xy_color"):
            if key in raw and raw[key] is not None:
                data[key] = raw[key]
    return data


def _ha_error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.text.strip()
        if body:
            return body[:300]
    except Exception:
        pass
    return str(exc.response.status_code)


def _extract_password_from_message(user_text: str, decision: dict[str, Any]) -> str | None:
    pwd = decision.get("provided_password")
    if isinstance(pwd, str) and pwd.strip():
        return pwd.strip()
    text = user_text.strip()
    if re.fullmatch(r"\d{4,8}", text):
        return text
    return None


async def _execute_unlock_pending(
    phone: str,
    pending: PendingUnlock,
    *,
    ha: HomeAssistantClient,
) -> str:
    log.info(
        "Executando unlock pendente phone=%s entity=%s %s/%s",
        phone,
        pending.entity_id,
        pending.domain,
        pending.service,
    )
    _log_service_payload("unlock pendente", pending.service_data)
    try:
        await ha.call_service(pending.domain, pending.service, pending.service_data)
        _pending_unlock.pop(phone, None)
        log.info("Unlock OK phone=%s entity=%s", phone, pending.entity_id)
        return "Porta destrancada com sucesso."
    except httpx.HTTPStatusError as e:
        log.warning(
            "Unlock falhou phone=%s entity=%s status=%s body=%s",
            phone,
            pending.entity_id,
            e.response.status_code,
            e.response.text[:500],
        )
        return "Não foi possível destrancar a porta. Tente novamente em instantes."


async def try_handle_pending_password(
    phone: str,
    user_text: str,
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    message_record: dict[str, Any] | None = None,
    evo: EvolutionClient | None = None,
    evo_base: str = "",
    evo_key: str = "",
    instance: str = "",
) -> str | None:
    """Se ha destrancar pendente, tenta validar senha. Retorna resposta ou None."""
    pending = _pending_unlock.get(phone)
    if not pending:
        return None

    log.info(
        "Senha pendente phone=%s entity=%s servico=%s/%s",
        phone,
        pending.entity_id,
        pending.domain,
        pending.service,
    )
    candidate = user_text.strip()
    if not catalog.verify_password(pending.entity_id, candidate):
        log.info("Senha incorreta phone=%s entity=%s (len=%s)", phone, pending.entity_id, len(candidate))
        return "Senha incorreta. Tente novamente ou cancele enviando outro comando."

    log.info("Senha OK phone=%s entity=%s, executando unlock", phone, pending.entity_id)
    reply = await _execute_unlock_pending(phone, pending, ha=ha)
    return reply


def build_gemini_assistant(
    settings: AppSettings,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog,
    cache_name: str | None = None,
) -> GeminiAssistant:
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    if cache_name is None and settings.gemini_api_key and (catalog.devices or cameras.cameras):
        cache_name = ensure_catalog_cache(
            api_key=settings.gemini_api_key,
            model=model_name,
            catalog=catalog,
            cameras=cameras,
            ttl_hours=settings.gemini_cache_ttl_hours,
        )
    fallback = ""
    if not cache_name:
        fallback = catalog.build_catalog_context()
        if cameras.cameras:
            fallback = f"{fallback}\n\n{cameras.build_catalog_context()}"
    return GeminiAssistant(
        settings.gemini_api_key,
        model=model_name,
        cache_name=cache_name,
        catalog_fallback=fallback,
    )


def _build_catalog_system_text(
    catalog: DevicesCatalog, cameras: CamerasCatalog
) -> str:
    from app.prompts import SYSTEM_INSTRUCTION

    catalog_text = catalog.build_catalog_context()
    if cameras.cameras:
        catalog_text = f"{catalog_text}\n\n{cameras.build_catalog_context()}"
    return f"{SYSTEM_INSTRUCTION}\n\n{catalog_text}"


def _build_catalog_fallback_text(
    catalog: DevicesCatalog, cameras: CamerasCatalog
) -> str:
    catalog_fallback = catalog.build_catalog_context()
    if cameras.cameras:
        catalog_fallback = f"{catalog_fallback}\n\n{cameras.build_catalog_context()}"
    return (
        f"{USER_MEMORY_ACTIONS_INSTRUCTION}\n\n"
        f"{INSTAGRAM_LINKS_ACTIONS_INSTRUCTION}\n\n"
        f"{FACT_CHECK_ACTIONS_INSTRUCTION}\n\n"
        f"{GOOGLE_CALENDAR_ACTIONS_INSTRUCTION}\n\n"
        f"{BIRTHDAY_ACTIONS_INSTRUCTION}\n\n"
        f"{catalog_fallback}"
    )


def _decision_is_complete(decision: dict[str, Any]) -> bool:
    batch_steps = split_gemini_decisions(decision)
    if len(batch_steps) > 1:
        return all(
            _normalize_action_name(row.get("action")) in VALID_GEMINI_ACTIONS
            for row in batch_steps
        )

    action = _normalize_action_name(decision.get("action"))
    reply = str(decision.get("response") or "").strip()
    if action in _SINGLE_USER_MESSAGE_ACTIONS and reply:
        return True
    if action in VALID_GEMINI_ACTIONS and action not in ("reply", "list_entities") and reply:
        return True
    if action == "reply" and reply:
        return (
            not _is_placeholder_scenario_response(reply)
            and not is_internal_instruction_leak(reply)
        )
    return False


def build_gemini_assistant_for_user(
    settings: AppSettings,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog,
    store: UserMemoryStore,
    *,
    catalog_cache_name: str | None = None,
) -> tuple[GeminiAssistant, str, bool]:
    """Retorna (assistant, memory_context, memory_in_cache)."""
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    memory_context = store.build_context_text()
    ig_context = get_instagram_store(store.phone).build_context_text()
    if ig_context:
        memory_context = f"{memory_context}\n\n{ig_context}" if memory_context else ig_context
    cal_context = get_google_calendar_store(store.phone).build_context_text()
    if cal_context:
        memory_context = f"{memory_context}\n\n{cal_context}" if memory_context else cal_context
    bday_context = get_birthday_store(store.phone).build_context_text()
    if bday_context:
        memory_context = f"{memory_context}\n\n{bday_context}" if memory_context else bday_context
    memory_in_cache = False
    catalog_system = _build_catalog_system_text(catalog, cameras)

    user_cache: str | None = None
    if (
        settings.gemini_api_key
        and memory_context
        and len(memory_context) >= _USER_MEMORY_CACHE_MIN_CHARS
    ):
        user_cache = ensure_user_memory_cache(
            api_key=settings.gemini_api_key,
            model=model_name,
            store=store,
            ttl_hours=settings.gemini_cache_ttl_hours,
            catalog_system_text=catalog_system,
        )
        if user_cache:
            memory_in_cache = True
            assistant = GeminiAssistant(
                settings.gemini_api_key,
                model=model_name,
                cache_name=user_cache,
                catalog_fallback="",
            )
            return assistant, memory_context, memory_in_cache

    cache_name = catalog_cache_name
    if cache_name is None and settings.gemini_api_key and (catalog.devices or cameras.cameras):
        cache_name = ensure_catalog_cache(
            api_key=settings.gemini_api_key,
            model=model_name,
            catalog=catalog,
            cameras=cameras,
            ttl_hours=settings.gemini_cache_ttl_hours,
        )

    catalog_fallback = ""
    if not cache_name:
        catalog_fallback = _build_catalog_fallback_text(catalog, cameras)

    assistant = GeminiAssistant(
        settings.gemini_api_key,
        model=model_name,
        cache_name=cache_name,
        catalog_fallback=catalog_fallback,
    )
    return assistant, memory_context, memory_in_cache


def _personal_label_from_inbound(content: InboundContent) -> str:
    parts: list[str] = []
    if content.media and content.media.caption:
        parts.append(content.media.caption)
    if not is_placeholder_user_text(content.text):
        parts.append(content.text)
    combined = " ".join(parts).strip()
    return extract_personal_description(combined, content.media.caption if content.media else "")


def _personal_save_confirmation(entry_label: str) -> str:
    return (
        f"Guardei no seu registro pessoal: *{entry_label}*. "
        "Quando quiser, peça para eu reenviar."
    )


async def _commit_bytes_to_personal_memory(
    phone: str,
    raw: bytes,
    *,
    filename: str,
    mime_type: str,
    caption: str = "",
    label: str = "",
) -> str:
    description = label.strip() or extract_personal_description("", caption)
    if not description:
        return build_personal_description_prompt()

    store = get_store(phone)
    entry = store.save_file(
        raw,
        filename=filename,
        mime_type=mime_type,
        label=description,
        caption=caption,
    )
    invalidate_user_memory_cache(store)
    display = entry.label or description
    return _personal_save_confirmation(display)


def _save_pending_bytes_to_personal(
    store: UserMemoryStore,
    pending,
    path,
    description: str,
    *,
    clear_pending: bool = True,
) -> str:
    raw = path.read_bytes()
    entry = store.save_file(
        raw,
        filename=pending.filename,
        mime_type=pending.mime_type,
        label=description[:120],
        caption=pending.caption,
    )
    if clear_pending:
        store.clear_pending_file()
    invalidate_user_memory_cache(store)
    display = entry.label or description
    return _personal_save_confirmation(display)


async def _upload_bytes_to_photoprism(
    raw: bytes,
    *,
    filename: str,
    album: str,
    mime_type: str = "",
    settings: AppSettings,
    http: httpx.AsyncClient,
) -> str:
    return await _upload_many_to_photoprism(
        [(raw, filename, mime_type)],
        album=album,
        settings=settings,
        http=http,
    )


async def _upload_many_to_photoprism(
    files: list[tuple[bytes, str, str]],
    *,
    album: str,
    settings: AppSettings,
    http: httpx.AsyncClient,
) -> str:
    if not settings.photoprism_url or not settings.photoprism_token:
        return (
            "PhotoPrism não está configurado no add-on. "
            "Defina photoprism_url e photoprism_token nas opções."
        )
    pp = PhotoprismClient(
        http,
        base_url=settings.photoprism_url,
        token=settings.photoprism_token,
        api_prefix=settings.photoprism_api_prefix,
    )
    try:
        result = await pp.upload_media_files(
            files,
            album=album,
            supervisor_token=settings.supervisor_token,
        )
    except PhotoprismAuthError:
        return "Erro de autenticação no PhotoPrism. Verifique o token nas opções."
    except PhotoprismError as e:
        log.warning("Upload PhotoPrism falhou: %s", e)
        return f"Não consegui enviar ao PhotoPrism: {e}"

    return format_upload_user_message(result, album=album)


async def _save_inbound_media(
    content: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    instance: str,
) -> str | None:
    """Baixa midia do Evolution e grava na pasta do usuario. Retorna nota para o prompt."""
    if not content.media or not content.record:
        return None

    downloaded = await download_inbound_media_bytes(
        content, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return "O usuário enviou um arquivo, mas não foi possível baixá-lo para guardar."

    raw, mimetype, fname = downloaded
    media = content.media
    caption = media.caption or content.text
    label = _personal_label_from_inbound(content)
    try:
        store = get_store(content.phone)
        entry = store.save_file(
            raw,
            filename=fname or media.filename,
            mime_type=mimetype or media.mimetype,
            label=label,
            caption=caption,
        )
        invalidate_user_memory_cache(store)
        return (
            f"[Sistema] Arquivo guardado: id={entry.id}, nome={entry.filename}, "
            f"tamanho={entry.size_bytes} bytes. Legenda/caption: {caption or '(sem legenda)'}."
        )
    except ValueError as e:
        return f"[Sistema] Não foi possível guardar o arquivo: {e}"
    except OSError as e:
        log.warning("Falha ao gravar arquivo usuario %s: %s", content.phone, e)
        return "[Sistema] Erro ao gravar o arquivo no disco."


async def try_handle_pending_media_reply(
    phone: str,
    user_text: str,
    *,
    settings: AppSettings,
    http: httpx.AsyncClient | None,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    """Processa resposta do usuario apos pergunta sobre arquivo pendente."""
    store = get_store(phone)
    pending_items = store.get_pending_files()
    if not pending_items:
        return None

    stage = store.get_pending_stage() or "destination"
    pending_files = [p for p, _ in pending_items]
    total_count, has_video, gallery_count = pending_gallery_stats(pending_files)
    supports_gallery = gallery_count > 0

    if stage == "collecting":
        return build_pending_collecting_wait()

    if stage == "processing":
        return build_pending_processing_wait()

    if stage == "description":
        if any(w in user_text.casefold() for w in ("cancela", "cancelar", "esquece", "descarta", "deixa")):
            cancel_media_batch_notification(phone)
            store.clear_pending_file()
            return "Ok, descartei os arquivos."
        first = pending_files[0]
        description = extract_personal_description(user_text, first.caption)
        if not description:
            return build_personal_description_prompt()
        store.set_pending_stage("processing")
        if on_step:
            await on_step(
                build_pending_progress_message("personal", count=total_count)
            )
        saved = 0
        try:
            for i, (pending, path) in enumerate(pending_items):
                _save_pending_bytes_to_personal(
                    store,
                    pending,
                    path,
                    description,
                    clear_pending=(i == len(pending_items) - 1),
                )
                saved += 1
        except ValueError as e:
            store.clear_pending_file()
            return f"Não foi possível guardar: {e}"
        except OSError:
            store.clear_pending_file()
            return "Erro ao gravar o arquivo."
        if saved > 1:
            return f"Guardei {saved} arquivos no seu registro pessoal."
        return _personal_save_confirmation(description)

    choice = classify_pending_reply(user_text, supports_gallery=supports_gallery)

    if choice == "cancel":
        cancel_media_batch_notification(phone)
        store.clear_pending_file()
        return "Ok, descartei os arquivos."

    if choice == "unknown":
        return build_pending_clarification(supports_gallery=supports_gallery)

    if choice == "photoprism":
        gallery_items = [
            (path.read_bytes(), pending.filename, pending.mime_type)
            for pending, path in pending_items
            if is_gallery_media(pending.mediatype, pending.mime_type)
        ]
        if not gallery_items:
            return (
                "Só posso enviar *fotos e vídeos* ao PhotoPrism. "
                "Para estes arquivos, responda *pessoal*."
            )
        if http is None:
            return "Serviço temporariamente indisponível para PhotoPrism."
        album = extract_album_name(user_text)
        store.set_pending_stage("processing")
        if on_step:
            await on_step(
                build_pending_progress_message(
                    "photoprism",
                    album=album,
                    count=len(gallery_items),
                    has_video=has_video,
                )
            )
        msg = await _upload_many_to_photoprism(
            gallery_items,
            album=album,
            settings=settings,
            http=http,
        )
        non_gallery = [
            item
            for item in pending_items
            if not is_gallery_media(item[0].mediatype, item[0].mime_type)
        ]
        store.clear_pending_file()
        if non_gallery:
            for pending, path in non_gallery:
                try:
                    store.append_pending_file(
                        path.read_bytes(),
                        filename=pending.filename,
                        mime_type=pending.mime_type,
                        mediatype=pending.mediatype,
                        caption=pending.caption,
                    )
                except ValueError:
                    pass
            return (
                f"{msg}\n\nAinda restam {len(non_gallery)} arquivo(s) que não vão "
                "para a galeria. Responda *pessoal* para guardar."
            )
        return msg

    first = pending_files[0]
    label = extract_personal_description(user_text, first.caption)
    if not label:
        store.set_pending_stage("description")
        return build_personal_description_prompt()

    store.set_pending_stage("processing")
    if on_step:
        await on_step(build_pending_progress_message("personal", count=total_count))
    saved = 0
    try:
        for i, (pending, path) in enumerate(pending_items):
            _save_pending_bytes_to_personal(
                store,
                pending,
                path,
                label,
                clear_pending=(i == len(pending_items) - 1),
            )
            saved += 1
    except ValueError as e:
        store.clear_pending_file()
        return f"Não foi possível guardar: {e}"
    except OSError:
        store.clear_pending_file()
        return "Erro ao gravar o arquivo."
    if saved > 1:
        return f"Guardei {saved} arquivos no seu registro pessoal."
    return _personal_save_confirmation(label)


async def handle_ambiguous_inbound_media(
    inbound: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    instance: str,
) -> str | None:
    """
    Arquivo sem instrucao: baixa, guarda como pendente e retorna mensagem para o usuario.
    Fotos/videos sao agrupados (debounce) — retorna "" quando a mensagem sera enviada depois.
    None se nao aplicavel.
    """
    if not inbound.media:
        return None
    if not is_storable_file_media(inbound.media.mediatype, inbound.media.mimetype):
        log.debug(
            "Midia tipo=%s ignorada no fluxo de arquivo pessoal (ex.: audio)",
            inbound.media.mediatype,
        )
        return None
    if media_has_explicit_intent(inbound):
        return None

    downloaded = await download_inbound_media_bytes(
        inbound, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return "Recebi seu arquivo, mas não consegui baixá-lo. Tente enviar de novo."

    raw, mimetype, fname = downloaded
    media = inbound.media
    store = get_store(inbound.phone)

    if is_gallery_media(media.mediatype, mimetype or media.mimetype):
        async with _media_batch_lock(inbound.phone):
            stage_before = store.get_pending_stage()
            prompt_already_sent = stage_before == "destination"
            try:
                _pending, total = store.append_pending_file(
                    raw,
                    filename=fname or media.filename,
                    mime_type=mimetype or media.mimetype,
                    mediatype=media.mediatype,
                    caption=media.caption,
                )
            except ValueError as e:
                return f"Não foi possível receber o arquivo: {e}"

            if stage_before in (None, "collecting", "destination"):
                store.set_pending_stage("collecting")
            _enqueue_media_batch_notification(
                inbound.phone,
                prompt_already_sent=prompt_already_sent,
                settings=settings,
                evo=evo,
                instance=instance,
            )
        log.info(
            "Midia em lote phone=%s total=%s prompt_ja_enviado=%s",
            inbound.phone,
            total,
            prompt_already_sent,
        )
        return ""

    try:
        _pending, total = store.append_pending_file(
            raw,
            filename=fname or media.filename,
            mime_type=mimetype or media.mimetype,
            mediatype=media.mediatype,
            caption=media.caption,
        )
    except ValueError as e:
        return f"Não foi possível receber o arquivo: {e}"

    pending_items = store.get_pending_files()
    pending_files = [p for p, _ in pending_items]
    _, has_video, gallery_count = pending_gallery_stats(pending_files)

    store.set_pending_stage("destination")
    return build_media_choice_prompt(
        total_count=total,
        gallery_count=gallery_count,
        has_video=has_video,
    )


async def route_explicit_inbound_media(
    inbound: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient | None,
    instance: str,
) -> str | None:
    """Executa pedido explicito na legenda/texto (guardar ou PhotoPrism)."""
    if not inbound.media or not media_has_explicit_intent(inbound):
        return None
    if not is_storable_file_media(inbound.media.mediatype, inbound.media.mimetype):
        return None

    downloaded = await download_inbound_media_bytes(
        inbound, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return "Não consegui baixar o arquivo para processar seu pedido."

    raw, mimetype, fname = downloaded
    media = inbound.media
    intent = classify_explicit_media_intent(inbound)

    store = get_store(inbound.phone)

    if intent == "photoprism":
        if not is_gallery_media(media.mediatype, mimetype or media.mimetype):
            return (
                "PhotoPrism aceita fotos e vídeos. Para este arquivo use memória pessoal "
                '(responda "guardar" na legenda).'
            )
        if http is None:
            return "Serviço indisponível para envio ao PhotoPrism."
        combined = f"{inbound.media.caption} {inbound.text}".strip()
        album = extract_album_name(combined)
        store.clear_pending_file()
        return await _upload_bytes_to_photoprism(
            raw,
            filename=fname or media.filename,
            album=album,
            mime_type=mimetype or media.mimetype,
            settings=settings,
            http=http,
        )

    store.clear_pending_file()
    combined = " ".join(
        filter(
            None,
            [
                media.caption or "",
                "" if is_placeholder_user_text(inbound.text) else inbound.text,
            ],
        )
    ).strip()
    description = extract_personal_description(combined, media.caption or "")
    if not description:
        try:
            store.save_pending_file(
                raw,
                filename=fname or media.filename,
                mime_type=mimetype or media.mimetype,
                mediatype=media.mediatype,
                caption=media.caption,
            )
            store.set_pending_stage("description")
        except ValueError as e:
            return f"Não foi possível receber o arquivo: {e}"
        return build_personal_description_prompt()

    return await _commit_bytes_to_personal_memory(
        inbound.phone,
        raw,
        filename=fname or media.filename,
        mime_type=mimetype or media.mimetype,
        caption=media.caption or "",
        label=description,
    )


async def handle_send_user_file(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> str:
    store = get_store(phone)
    file_id = str(decision.get("file_id") or "").strip()
    file_name = str(decision.get("file_name") or "").strip()
    label = str(decision.get("memory_label") or "").strip()

    hit = store.find_file(file_id=file_id, filename=file_name, label=label)
    intro = str(decision.get("response") or "").strip()

    async def say(text: str) -> None:
        if messenger:
            await messenger.step(text)
        else:
            evo_base = settings.evolution_base_url.strip()
            evo_key = settings.evolution_api_key.strip()
            if evo_base and evo_key and instance:
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    text=_truncate_whatsapp(text),
                )

    if not hit:
        msg = intro or "Não encontrei esse arquivo na sua memória."
        if not hit and not intro:
            files = store.list_files()
            if files:
                names = ", ".join(f"{f.filename} (id={f.id})" for f in files[-5:])
                msg = f"Não achei o arquivo. Os últimos guardados: {names}."
        await say(msg)
        return msg

    meta, path = hit
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        return "Evolution não configurado para enviar o arquivo."

    if intro:
        await say(intro)

    data = path.read_bytes()
    caption = (meta.caption or meta.label or meta.filename)[:1024]
    await pulse_whatsapp_typing()
    ok = await evo.send_document_bytes(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        file_bytes=data,
        filename=meta.filename,
        caption=caption,
        mimetype=meta.mime_type,
    )
    if ok is None:
        msg = "Encontrei o arquivo mas não consegui enviar pelo WhatsApp."
        await say(msg)
        return msg
    return intro or f"Enviei o arquivo {meta.filename}."


async def handle_send_instagram_link(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> str:
    link_id = str(decision.get("instagram_link_id") or "").strip()
    handle = str(decision.get("instagram_handle") or "").strip().lstrip("@")
    raw_num = decision.get("instagram_list_number")
    num: int | None = None
    if isinstance(raw_num, int):
        num = raw_num
    elif isinstance(raw_num, str) and raw_num.strip().isdigit():
        num = int(raw_num.strip())

    ent = resolve_entry_for_reference(phone, link_id=link_id, handle=handle, list_number=num)
    intro = str(decision.get("response") or "").strip()

    async def say(text: str) -> None:
        if messenger:
            await messenger.step(text)
        else:
            evo_base = settings.evolution_base_url.strip()
            evo_key = settings.evolution_api_key.strip()
            if evo_base and evo_key and instance:
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    text=_truncate_whatsapp(text),
                )

    if not ent:
        msg = intro or "Nao encontrei esse perfil Instagram guardado."
        await say(msg)
        return msg

    store = get_instagram_store(phone)
    caption = _truncate_whatsapp(format_profile_summary(ent))
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        return "Evolution nao configurado para enviar o perfil."

    if intro:
        await say(intro)

    path = store.avatar_path(ent)
    await pulse_whatsapp_typing()
    if path:
        data = path.read_bytes()
        mime = "image/jpeg"
        if path.suffix.lower() == ".png":
            mime = "image/png"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        ok = await evo.send_image_bytes(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            image_bytes=data,
            filename=path.name,
            caption=caption,
            mimetype=mime,
        )
        if ok is None:
            msg = "Encontrei o perfil mas nao consegui enviar a foto."
            await say(msg)
            return msg
        return intro or f"Enviei o perfil @{ent.handle}."
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=caption,
    )
    return intro or f"Enviei o resumo de @{ent.handle}."


def handle_list_instagram_links(phone: str) -> str:
    from app.instagram_links_actions import format_instagram_links_list

    return format_instagram_links_list(phone)


async def handle_refresh_instagram_link(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
) -> str:
    link_id = str(decision.get("instagram_link_id") or "").strip()
    handle = str(decision.get("instagram_handle") or "").strip().lstrip("@")
    raw_num = decision.get("instagram_list_number")
    num: int | None = None
    if isinstance(raw_num, int):
        num = raw_num
    elif isinstance(raw_num, str) and raw_num.strip().isdigit():
        num = int(raw_num.strip())

    ent = resolve_entry_for_reference(phone, link_id=link_id, handle=handle, list_number=num)
    if not ent:
        reply = str(decision.get("response") or "").strip()
        return reply or "Nao encontrei esse perfil Instagram guardado."

    store = get_instagram_store(phone)
    ent.fetch_status = "pending"
    store.update_entry(ent)
    invalidate_user_memory_cache(get_store(phone))

    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if evo_base and evo_key and instance:
        asyncio.create_task(
            enrich_and_notify_instagram_profile(
                entry_id=ent.id,
                phone=phone,
                settings=settings,
                evo=evo,
                http=http,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
        )

    confirm = str(decision.get("response") or "").strip()
    return confirm or f"A atualizar @{ent.handle}. Envio o resumo atualizado em instantes."


def handle_save_memory(decision: dict[str, Any], phone: str) -> str:
    from app.vault_credential_detection import memory_decision_looks_like_vault

    if memory_decision_looks_like_vault(decision):
        return (
            "Credenciais devem ir para o cofre encriptado, não para a memória pessoal. "
            "Peça para *guardar senha* ou repita o pedido."
        )
    text = str(decision.get("memory_text") or "").strip()
    if not text:
        reply = str(decision.get("response") or "").strip()
        return reply or "Não entendi o que devo guardar na memória."
    label = str(decision.get("memory_label") or "").strip()
    store = get_store(phone)
    store.add_memory(text, label=label)
    invalidate_user_memory_cache(store)
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    short = text if len(text) <= 80 else text[:77] + "..."
    return f"Guardei na sua memória: {short}"


async def handle_schedule_response(
    decision: dict[str, Any],
    *,
    phone: str,
    ha: HomeAssistantClient,
) -> str:
    context = str(decision.get("context") or "").strip()
    if not context:
        reply = str(decision.get("response") or "").strip()
        return reply or "Não entendi o que devo agendar."

    trigger_type = str(decision.get("trigger_type") or "entity").lower()
    if trigger_type not in ("time", "entity"):
        return "Tipo de agendamento inválido."

    label = str(decision.get("label") or decision.get("schedule_label") or "").strip()
    trigger_on = str(decision.get("trigger_on") or "enter").lower()
    if trigger_on not in ("enter", "match"):
        trigger_on = "enter"

    entities_raw = decision.get("context_entities")
    context_entities: list[str] = []
    if isinstance(entities_raw, list):
        for e in entities_raw:
            if isinstance(e, str) and e.strip():
                context_entities.append(e.strip())

    store = get_scheduled_store(phone)
    last_known_state: str | None = None
    entity_id: str | None = None
    when_state: str | None = None
    fire_at: str | None = None
    fire_after_seconds: int | None = None

    if trigger_type == "entity":
        entity_id = str(decision.get("entity_id") or "").strip()
        when_state = str(decision.get("when_state") or "").strip()
        if not entity_id or not when_state:
            return "Para agendar por entidade, preciso do aparelho e da condição."
        st = await ha.get_state(entity_id)
        if st:
            last_known_state = str(st.get("state", ""))
        if entity_id not in context_entities:
            context_entities.insert(0, entity_id)
    else:
        raw_fire_at = decision.get("fire_at")
        if isinstance(raw_fire_at, str) and raw_fire_at.strip():
            fire_at = raw_fire_at.strip()
        raw_after = decision.get("fire_after_seconds")
        if isinstance(raw_after, (int, float)):
            fire_after_seconds = int(raw_after)
        elif isinstance(raw_after, str) and raw_after.strip().isdigit():
            fire_after_seconds = int(raw_after.strip())
        if not fire_at and not fire_after_seconds:
            return "Para agendar por tempo, preciso de fire_at ou fire_after_seconds."

    try:
        entry = store.add(
            context=context,
            trigger_type=trigger_type,  # type: ignore[arg-type]
            label=label,
            fire_at=fire_at,
            fire_after_seconds=fire_after_seconds,
            entity_id=entity_id,
            when_state=when_state,
            trigger_on=trigger_on,  # type: ignore[arg-type]
            context_entities=context_entities,
            last_known_state=last_known_state,
        )
    except ValueError as e:
        return str(e)

    ensure_runner_started()
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    return f"Agendado (id {entry.id}): {entry.summary()}."


async def handle_schedule_action(
    decision: dict[str, Any],
    *,
    phone: str,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
) -> str:
    domain = str(decision.get("domain") or "").strip()
    service = str(decision.get("service") or "").strip()
    if not domain or not service:
        reply = str(decision.get("response") or "").strip()
        return reply or "Comando incompleto para agendar ação (domain/service)."

    raw_svc_data = decision.get("service_data")
    if raw_svc_data is not None and not isinstance(raw_svc_data, dict):
        return "service_data inválido."
    raw_svc_data = dict(raw_svc_data) if isinstance(raw_svc_data, dict) else {}

    target = extract_target_entity_id(domain, service, raw_svc_data, decision.get("entity_id"))
    if not target:
        return "Informe entity_id no comando agendado."

    allowed = catalog.actionable_entity_ids()
    if not allowed:
        return "Ainda não tenho nenhum aparelho autorizado para controlar por aqui."
    if target not in allowed:
        return (
            "Não tenho permissão para agendar alteração nesse aparelho. "
            "Só posso controlar o que está na lista de dispositivos do assistente."
        )

    if catalog.service_requires_password(target, service):
        return (
            "Não consigo agendar ações que exigem senha. "
            "Faça agora ou peça para executar imediatamente."
        )

    svc_data = build_ha_service_data(domain, service, target, raw_svc_data)
    svc_data = catalog.apply_service_defaults(target, svc_data)

    context = str(decision.get("context") or "").strip()
    if not context:
        context = f"Agendar {domain}/{service} em {target}"

    label = str(decision.get("label") or decision.get("schedule_label") or "").strip()
    trigger_type = str(decision.get("trigger_type") or "time").lower()
    if trigger_type not in ("time", "entity"):
        trigger_type = "time"
    trigger_on = str(decision.get("trigger_on") or "enter").lower()
    if trigger_on not in ("enter", "match"):
        trigger_on = "enter"

    entities_raw = decision.get("context_entities")
    context_entities: list[str] = []
    if isinstance(entities_raw, list):
        for e in entities_raw:
            if isinstance(e, str) and e.strip():
                context_entities.append(e.strip())
    if target not in context_entities:
        context_entities.insert(0, target)

    store = get_scheduled_store(phone)
    last_known_state: str | None = None
    entity_id: str | None = None
    when_state: str | None = None
    fire_at: str | None = None
    fire_after_seconds: int | None = None

    if trigger_type == "entity":
        entity_id = str(decision.get("entity_id") or "").strip()
        when_state = str(decision.get("when_state") or "").strip()
        if not entity_id or not when_state:
            return "Para agendar ação por entidade, preciso do gatilho (entity_id e when_state)."
        st = await ha.get_state(entity_id)
        if st:
            last_known_state = str(st.get("state", ""))
        if entity_id not in context_entities:
            context_entities.insert(0, entity_id)
    else:
        raw_fire_at = decision.get("fire_at")
        if isinstance(raw_fire_at, str) and raw_fire_at.strip():
            fire_at = raw_fire_at.strip()
        raw_after = decision.get("fire_after_seconds")
        if isinstance(raw_after, (int, float)):
            fire_after_seconds = int(raw_after)
        elif isinstance(raw_after, str) and raw_after.strip().isdigit():
            fire_after_seconds = int(raw_after.strip())
        if not fire_at and not fire_after_seconds:
            return "Para agendar ação, preciso de fire_after_seconds ou fire_at."
        if fire_after_seconds is not None and fire_after_seconds < MIN_ACTION_DELAY_SECONDS:
            mins = max(1, MIN_ACTION_DELAY_SECONDS // 60)
            return f"Agendamento minimo de {mins} minuto(s)."

    try:
        entry = store.add(
            context=context,
            trigger_type=trigger_type,  # type: ignore[arg-type]
            label=label,
            kind="action",
            fire_at=fire_at,
            fire_after_seconds=fire_after_seconds,
            entity_id=entity_id,
            when_state=when_state,
            trigger_on=trigger_on,  # type: ignore[arg-type]
            context_entities=context_entities,
            last_known_state=last_known_state,
            action_domain=domain,
            action_service=service,
            action_service_data=svc_data,
            action_entity_id=target,
        )
    except ValueError as e:
        return str(e)

    ensure_runner_started()
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    return f"Ação agendada (id {entry.id}): {entry.summary()}."


async def _maybe_auto_schedule_boiler_ready(
    *,
    phone: str,
    ha: HomeAssistantClient,
    target: str,
    domain: str,
    service: str,
    svc_data: dict[str, Any],
) -> str | None:
    """Apos ligar o boiler, agenda aviso quando a temperatura atingir 42C."""
    if target != BOILER_MODE_ENTITY:
        return None
    if domain != "input_select" or service != "select_option":
        return None
    option = str(svc_data.get("option") or "").strip().lower()
    if option != "ligado":
        return None

    store = get_scheduled_store(phone)
    existing = store.find_by_label(BOILER_SCHEDULE_LABEL)
    if existing and existing.is_pending():
        return None

    st = await ha.get_state(BOILER_TEMP_ENTITY)
    last_known = str(st.get("state", "")) if st else None
    if last_known and state_matches(last_known, BOILER_READY_WHEN):
        return None

    try:
        store.add(
            context=(
                "Usuario pediu aquecer a agua do boiler para banho; "
                "avisar quando atingir 42 graus C ou mais."
            ),
            trigger_type="entity",
            label=BOILER_SCHEDULE_LABEL,
            entity_id=BOILER_TEMP_ENTITY,
            when_state=BOILER_READY_WHEN,
            trigger_on="enter",
            context_entities=[BOILER_TEMP_ENTITY, BOILER_MODE_ENTITY],
            last_known_state=last_known,
        )
    except ValueError:
        return None

    ensure_runner_started()
    return "Te aviso quando a agua chegar a 42 graus."


def handle_cancel_scheduled_response(decision: dict[str, Any], phone: str) -> str:
    store = get_scheduled_store(phone)
    schedule_id = str(decision.get("schedule_id") or "").strip()
    schedule_label = str(decision.get("schedule_label") or decision.get("label") or "").strip()

    target = store.get_by_id(schedule_id) if schedule_id else None
    if not target and schedule_label:
        target = store.find_by_label(schedule_label)

    if not target or not target.is_pending():
        reply = str(decision.get("response") or "").strip()
        return reply or "Não encontrei nenhum agendamento pendente com esse identificador."

    store.cancel(target.id)
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    desc = target.label or target.context[:60]
    return f"Cancelado o agendamento: {desc}."


async def execute_decision_batch(
    decisions: list[dict[str, Any]],
    *,
    phone: str,
    messenger: StepMessenger | None = None,
    ha: HomeAssistantClient | None = None,
    catalog: DevicesCatalog | None = None,
    user_text: str = "",
    entities_context_used: bool = True,
    message_record: dict[str, Any] | None = None,
    evo: EvolutionClient | None = None,
    evo_base: str = "",
    evo_key: str = "",
    instance: str = "",
    settings: AppSettings | None = None,
) -> str:
    """Executa varias decisoes Gemini da mesma mensagem (ex.: lista de aniversarios)."""
    if not decisions:
        return "Nao entendi o pedido."

    actions = {_normalize_action_name(d.get("action")) for d in decisions}
    if actions == {"birthday_save"}:
        text = execute_birthday_save_batch(decisions, phone)
        out = polish_user_message(text) or "Feito."
        if messenger:
            await messenger.step(out, final=True)
            return ""
        return out

    parts: list[str] = []
    for item in decisions:
        if ha is None or catalog is None:
            break
        part = await execute_decision(
            item,
            ha=ha,
            catalog=catalog,
            phone=phone,
            user_text=user_text,
            entities_context_used=entities_context_used,
            messenger=None,
            message_record=message_record,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            settings=settings,
        )
        if part and part.strip():
            parts.append(part.strip())

    out = polish_user_message("\n\n".join(parts)) or "Feito."
    if messenger:
        await messenger.step(out, final=True)
        return ""
    return out


async def execute_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    phone: str,
    user_text: str,
    entities_context_used: bool,
    messenger: StepMessenger | None = None,
    message_record: dict[str, Any] | None = None,
    evo: EvolutionClient | None = None,
    evo_base: str = "",
    evo_key: str = "",
    instance: str = "",
    settings: AppSettings | None = None,
) -> str:
    action = _normalize_action_name(decision.get("action"))
    if action not in VALID_GEMINI_ACTIONS:
        log.warning("execute_decision: acao '%s' invalida; tratando como reply", action)
        action = "reply"
    reply = str(decision.get("response") or "").strip()
    log.info("execute_decision phone=%s action=%s steps=%s", phone, action, bool(messenger))

    async def deliver(text: str, *, final: bool = False) -> None:
        t = polish_user_message(text)
        if t and messenger:
            await messenger.step(t, final=final)

    async def send_user_message(message: str) -> str:
        """Envia uma unica mensagem (sem concatenar reply + result)."""
        out = polish_user_message(message) or polish_user_message(reply) or "Feito."
        if messenger:
            if out:
                await messenger.step(out, final=True)
            return ""
        return out

    async def finalize(extra: str) -> str:
        if action in _SINGLE_USER_MESSAGE_ACTIONS:
            return await send_user_message(extra)
        base = polish_user_message(reply)
        extra_clean = polish_user_message(extra)
        if extra_clean and base and extra_clean == base:
            extra_clean = ""
        msg = (base + ("\n\n" + extra_clean if extra_clean else "")).strip()
        out = msg or extra_clean or "Feito."
        if messenger:
            if out:
                await messenger.step(out, final=True)
            return ""
        return out

    _NO_EARLY_REPLY_ACTIONS = frozenset(
        {
            "reply",
            "list_entities",
            "get_state",
            "call_service",
        }
    ) | _SINGLE_USER_MESSAGE_ACTIONS
    if (
        messenger
        and reply
        and not _is_placeholder_scenario_response(reply)
        and action not in _NO_EARLY_REPLY_ACTIONS
    ):
        await deliver(reply)

    if action in ("reply", "list_entities"):
        if not reply and entities_context_used:
            return await finalize("Ok.")
        if not reply and messenger:
            return ""
        return await finalize("")

    if action == "get_state":
        eid = decision.get("entity_id")
        if not isinstance(eid, str) or not eid.strip():
            return await finalize("Não entendi qual aparelho devo verificar.")
        eid = eid.strip()
        st = await ha.get_state(eid)
        if not st:
            return await finalize("Não encontrei esse aparelho na casa.")
        result = format_state_value(eid, st, catalog)
        if messenger:
            if _substantive_reply(reply):
                out = (
                    reply
                    if _messages_are_redundant(reply, result)
                    else f"{reply.strip()}\n\n{result}".strip()
                )
            else:
                out = result
            await deliver(out, final=True)
            return ""
        if _substantive_reply(reply) and _messages_are_redundant(reply, result):
            return await finalize(reply)
        return await finalize(result)

    if action == "call_service":
        domain = decision.get("domain")
        service = decision.get("service")
        svc_data = decision.get("service_data")
        if not isinstance(domain, str) or not isinstance(service, str):
            return await finalize("Comando incompleto (domain/service).")
        if svc_data is not None and not isinstance(svc_data, dict):
            return await finalize("service_data inválido.")

        domain = domain.strip()
        service = service.strip()
        raw_svc_data = dict(svc_data) if isinstance(svc_data, dict) else {}

        target = extract_target_entity_id(domain, service, raw_svc_data, decision.get("entity_id"))
        targets = extract_target_entity_ids(domain, service, raw_svc_data, decision.get("entity_id"))
        if not target:
            return await finalize("Informe entity_id no comando.")

        svc_data = build_ha_service_data(domain, service, target, raw_svc_data)
        svc_data = catalog.apply_service_defaults(target, svc_data)
        if raw_svc_data != svc_data:
            log.info(
                "service_data normalizado entity=%s raw=%s -> ha=%s",
                target,
                raw_svc_data,
                svc_data,
            )

        allowed = catalog.actionable_entity_ids()
        if not allowed:
            return await finalize(
                "Ainda não tenho nenhum aparelho autorizado para controlar por aqui."
            )
        disallowed = [t for t in targets if t not in allowed]
        if disallowed:
            return await finalize(
                "Não tenho permissão para alterar esse aparelho. "
                "Só posso controlar o que está na lista de dispositivos do assistente."
            )

        if catalog.service_requires_password(target, service):
            password = _extract_password_from_message(user_text, decision)
            has_pwd = bool(password)
            pwd_ok = bool(password and catalog.verify_password(target, password))
            log.info(
                "Seguranca entity=%s servico=%s tem_senha=%s senha_ok=%s provided_password=%s",
                target,
                service,
                has_pwd,
                pwd_ok,
                bool(decision.get("provided_password")),
            )
            if not password or not pwd_ok:
                _pending_unlock[phone] = PendingUnlock(
                    entity_id=target,
                    domain=domain,
                    service=service,
                    service_data=svc_data,
                )
                log.info("Unlock pendente registrado phone=%s entity=%s", phone, target)
                prompt = catalog.password_prompt_for(target)
                msg = _password_prompt_message(reply, prompt)
                if messenger:
                    await messenger.step(msg)
                    return ""
                return msg

        st_before = await ha.get_state(target)
        if not messenger:
            await deliver(
                format_action_in_progress(
                    domain, service, target, svc_data, catalog, st_before
                )
            )
        log.info("Chamando HA phone=%s %s/%s entity=%s", phone, domain, service, target)
        _log_service_payload("call_service", svc_data)
        try:
            result = await ha.call_service(domain, service, svc_data or None)
            _pending_unlock.pop(phone, None)
            log.info("HA call_service OK phone=%s %s/%s entity=%s", phone, domain, service, target)
            st_after = await ha.get_state(target)
            done = format_action_success(
                domain,
                service,
                target,
                svc_data,
                catalog,
                result=result,
                state_after=st_after,
            )
            notify_extra = await _maybe_auto_schedule_boiler_ready(
                phone=phone,
                ha=ha,
                target=target,
                domain=domain,
                service=service,
                svc_data=svc_data,
            )
            if notify_extra:
                done = f"{done}\n\n{notify_extra}"
            if messenger:
                await deliver(done, final=True)
                return ""
            return await finalize(done)
        except httpx.HTTPStatusError as e:
            log.warning(
                "HA call_service falhou phone=%s %s/%s entity=%s status=%s body=%s payload=%s",
                phone,
                domain,
                service,
                target,
                e.response.status_code,
                e.response.text[:500],
                svc_data,
            )
            return await finalize(format_ha_error_user())

    if action == "search_photos":
        return await finalize(
            "Pedido de fotos recebido. Se nada chegar, verifique PhotoPrism nas opções do add-on."
        )

    if action == "get_camera_snapshot":
        return await finalize(
            "Pedido de câmera recebido. Se nada chegar, verifique Frigate nas opções do add-on."
        )

    if action == "save_memory":
        from app.vault_credential_detection import (
            memory_decision_looks_like_vault,
            vault_fields_from_memory_decision,
        )

        if memory_decision_looks_like_vault(decision):
            if not evo or not evo_base or not evo_key or not instance:
                return await finalize(
                    "Credenciais vão para o cofre encriptado, mas o WhatsApp não está configurado."
                )
            vault_label, vault_secret = vault_fields_from_memory_decision(decision)
            vault_decision: dict[str, Any] = {
                "action": "vault_save",
                "vault_label": vault_label,
                "vault_secret": vault_secret,
                "response": str(decision.get("response") or "").strip(),
            }
            settings_key = settings.vault_master_key if settings else ""
            result, redact_user, redact_assistant = await handle_vault_gemini_decision(
                vault_decision,
                phone=phone,
                user_text=user_text,
                settings_key=settings_key,
                message_record=message_record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
                messenger=messenger,
            )
            if messenger and messenger.sent_any:
                record_exchange(
                    phone,
                    "[senha omitida]" if redact_user else user_text,
                    "Senha exibida (removida do chat por segurança)."
                    if redact_assistant
                    else messenger.combined(),
                )
                return ""
            return await finalize(result)
        result = handle_save_memory(decision, phone)
        return await finalize(result)

    if action == "send_user_file":
        return await finalize(
            "Arquivo solicitado. Se nada chegar, verifique se o id ou nome estao corretos."
        )

    if action == "delete_from_memory":
        result = handle_delete_from_memory(decision, phone)
        return await finalize(result)

    if action in ("vault_save", "vault_retrieve", "vault_list"):
        if not evo or not evo_base or not evo_key or not instance:
            return await finalize("Cofre de senhas indisponível (Evolution não configurado).")
        settings_key = settings.vault_master_key if settings else ""
        result, redact_user, redact_assistant = await handle_vault_gemini_decision(
            decision,
            phone=phone,
            user_text=user_text,
            settings_key=settings_key,
            message_record=message_record,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            messenger=messenger,
        )
        if messenger and messenger.sent_any:
            record_exchange(
                phone,
                "[senha omitida]" if redact_user else user_text,
                "Senha exibida (removida do chat por segurança)."
                if redact_assistant
                else messenger.combined(),
            )
            return ""
        return await finalize(result)

    if action == "schedule_response":
        result = await handle_schedule_response(decision, phone=phone, ha=ha)
        return await finalize(result)

    if action == "schedule_action":
        result = await handle_schedule_action(decision, phone=phone, ha=ha, catalog=catalog)
        return await finalize(result)

    if action == "cancel_scheduled_response":
        result = handle_cancel_scheduled_response(decision, phone)
        return await finalize(result)

    if action == "list_instagram_links":
        result = handle_list_instagram_links(phone)
        return await finalize(result)

    if action == "search_instagram_links":
        result = handle_search_instagram_links(decision, phone)
        return await finalize(result)

    if action == "delete_instagram_link":
        result = handle_delete_instagram_link(decision, phone)
        return await finalize(result)

    if action == "google_calendar_save_link":
        result = handle_google_calendar_save_link(decision, phone)
        ensure_google_calendar_runner_running()
        return await finalize(result)

    if action == "google_calendar_configure":
        result = handle_google_calendar_configure(decision, phone)
        ensure_google_calendar_runner_running()
        return await finalize(result)

    if action == "google_calendar_show_settings":
        result = handle_google_calendar_show_settings(phone)
        return await finalize(result)

    if action == "birthday_save":
        result = handle_birthday_save(decision, phone)
        ensure_birthday_runner_running()
        return await finalize(result)

    if action == "birthday_list":
        result = handle_birthday_list(phone)
        return await finalize(result)

    if action == "birthday_upcoming":
        result = handle_birthday_upcoming(decision, phone)
        return await finalize(result)

    if action == "birthday_delete":
        result = handle_birthday_delete(decision, phone)
        return await finalize(result)

    fallback = reply or "Não entendi o próximo passo."
    if messenger:
        await deliver(fallback)
        return ""
    return fallback


def _parse_photo_filters(decision: dict[str, Any]) -> dict[str, Any]:
    raw = decision.get("filters")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "person",
        "people",
        "city",
        "country",
        "state",
        "label",
        "keywords",
        "album",
        "query",
        "after",
        "before",
        "taken",
    ):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    variants = raw.get("city_variants")
    if isinstance(variants, list):
        out["city_variants"] = [str(v).strip() for v in variants if isinstance(v, str) and v.strip()]
    people_list = raw.get("people_list")
    if isinstance(people_list, list):
        out["people_list"] = [str(v).strip() for v in people_list if isinstance(v, str) and v.strip()]
    mode = raw.get("people_mode")
    if isinstance(mode, str) and mode.strip().lower() in ("all", "any"):
        out["people_mode"] = mode.strip().lower()
    for key in ("year", "month", "day"):
        val = raw.get(key)
        if isinstance(val, int):
            out[key] = val
        elif isinstance(val, str) and val.strip().isdigit():
            out[key] = int(val.strip())
    return out


def _parse_photo_count(decision: dict[str, Any], default: int, maximum: int) -> int:
    count = decision.get("count")
    if isinstance(count, int):
        n = count
    elif isinstance(count, str) and count.strip().isdigit():
        n = int(count.strip())
    else:
        n = default
    return max(1, min(n, maximum))


def _photo_caption(photo: PhotoResult, index: int, total: int) -> str:
    parts: list[str] = []
    if photo.title:
        parts.append(photo.title)
    if photo.taken_at:
        parts.append(photo.taken_at[:10] if len(photo.taken_at) >= 10 else photo.taken_at)
    if photo.place_label:
        parts.append(photo.place_label)
    base = " — ".join(parts) if parts else f"Foto {index}/{total}"
    return f"{index}/{total}: {base}"[:1024]


async def handle_get_camera_snapshot(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    cameras: CamerasCatalog,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> None:
    """Obtem snapshot(s) do Frigate e envia pelo WhatsApp."""
    await handle_camera_snapshot_decision(
        decision,
        settings=settings,
        cameras=cameras,
        evo=evo,
        http=http,
        phone=phone,
        instance=instance,
        messenger=messenger,
    )


async def handle_search_photos(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> None:
    """Busca fotos no PhotoPrism e envia pelo WhatsApp."""
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        log.error("Envio de fotos bloqueado: Evolution nao configurado")
        return

    intro = str(decision.get("response") or "").strip()

    async def say(text: str) -> None:
        if messenger:
            await messenger.step(text)
        else:
            await evo.send_text(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                text=_truncate_whatsapp(text),
            )

    if not settings.photoprism_url or not settings.photoprism_token:
        msg = (
            intro + "\n\nPhotoPrism não configurado. "
            "Defina photoprism_url e photoprism_token nas opções do add-on."
        ).strip()
        await say(msg)
        return

    if intro:
        await say(intro)

    filters = normalize_photo_filters(_parse_photo_filters(decision))
    count = _parse_photo_count(
        decision,
        default=5,
        maximum=settings.photoprism_max_photos,
    )
    pp = PhotoprismClient(
        http,
        base_url=settings.photoprism_url,
        token=settings.photoprism_token,
        api_prefix=settings.photoprism_api_prefix,
    )
    log.info(
        "PhotoPrism busca: url=%s api_base=%s count=%s filters=%s",
        settings.photoprism_url,
        pp.api_base,
        count,
        filters,
    )

    await say("A procurar fotos no acervo PhotoPrism...")

    people_names = filters.get("_people_names")
    people_mode = filters.get("_people_mode")
    if isinstance(people_names, list) and len(people_names) > 1:
        joined = ", ".join(str(n) for n in people_names)
        if people_mode == "any":
            await say(f"Buscando fotos com qualquer uma destas pessoas: {joined}.")
        else:
            await say(f"Buscando fotos com todas estas pessoas juntas: {joined}.")

    pp_label = filters.get("label") if isinstance(filters.get("label"), str) else ""
    if pp_label.strip():
        await say(
            f"Filtrando pelas etiquetas PhotoPrism: {pp_label.replace('|', ', ')}."
        )

    city = filters.get("city") if isinstance(filters.get("city"), str) else ""
    place_terms = expand_city_variants(city) if city else []
    if city and len(place_terms) > 1:
        others = [t for t in place_terms if t.casefold() != city.casefold()]
        if others:
            await say(
                f"Incluindo variantes do local: {city} / {others[0]}"
                + (f" (+{len(others) - 1})" if len(others) > 1 else "")
                + "."
            )

    photos: list[PhotoResult] = []
    preview_token: str | None = None
    attempts = build_search_attempts(filters)

    try:
        for i, (label, attempt_filters, client_place) in enumerate(attempts):
            if i > 0:
                if client_place and city:
                    await say(
                        f"Nada na busca anterior. Tentando {label}..."
                    )
                else:
                    await say(f"Tentando outra busca ({label})...")

            batch, preview_token = await pp.search_photos(
                filters=attempt_filters,
                count=count,
                supervisor_token=settings.supervisor_token,
            )
            if client_place and city:
                batch = [p for p in batch if photo_matches_place(p, place_terms)]
                batch = batch[:count]

            if batch:
                photos = batch
                log.info(
                    "PhotoPrism busca OK tentativa=%s resultados=%s filtros=%s",
                    label,
                    len(photos),
                    attempt_filters,
                )
                break
    except PhotoprismAuthError:
        await say("Erro de autenticação no PhotoPrism. Verifique o token.")
        return
    except PhotoprismError as e:
        log.error("PhotoPrism falhou: %s", e, exc_info=True)
        extra = ""
        diag = getattr(e, "diagnostic", None) or {}
        if isinstance(diag, dict) and diag.get("hints"):
            extra = f" {diag['hints'][0]}"
        await say(f"Não consegui buscar fotos: {e}{extra}")
        return

    if not photos:
        detail = ""
        if city:
            detail = f" (pessoa + {city})"
        await say(f"Não encontrei fotos com esses critérios{detail}.")
        return

    await say(f"Encontrei {len(photos)} foto(s). A enviar...")

    token = preview_token or "public"
    sent = 0
    total = len(photos)
    for i, photo in enumerate(photos, start=1):
        try:
            data = await pp.get_thumbnail_bytes(
                file_hash=photo.file_hash,
                preview_token=token,
            )
        except PhotoprismError as e:
            log.warning("Thumbnail falhou %s: %s", photo.file_hash[:12], e)
            continue

        caption = _photo_caption(photo, i, total)
        fname = f"shakira_{photo.uid or photo.file_hash[:8]}_{i}.jpg"
        await pulse_whatsapp_typing()
        ok = await evo.send_image_bytes(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            image_bytes=data,
            filename=fname,
            caption=caption,
        )
        if ok is not None:
            sent += 1
        if i < total:
            await asyncio.sleep(0.5)

    if sent == 0:
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text="Encontrei fotos mas não consegui enviar as imagens.",
        )
    elif sent < total:
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=f"Enviei {sent} de {total} fotos.",
        )


async def handle_evolution_payload(
    payload: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    settings: AppSettings,
    gemini_cache_name: str | None = None,
    http: httpx.AsyncClient | None = None,
    catalog: DevicesCatalog | None = None,
    cameras: CamerasCatalog | None = None,
) -> None:
    gemini_key = settings.gemini_api_key.strip()
    if not gemini_key:
        log.warning("Chave Gemini ausente nas opcoes do add-on")
        return

    if catalog is None:
        catalog = DevicesCatalog.load(settings.devices_config_path)
    if cameras is None:
        cameras = CamerasCatalog.load(settings.frigate_cameras_config_path)
    permitted_raw = await fetch_permitted_phones_raw(ha)
    permitted = parse_allowed_numbers(permitted_raw)
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    default_inst = settings.evolution_instance.strip()

    if not permitted:
        log.warning("Nenhum numero permitido em %s", ENTITY_PERMITTED)
    if not evo_base or not evo_key:
        log.warning("Evolution URL ou api key ausentes nas opcoes do add-on")

    normalized = normalize_evolution_payload(payload)
    webhook_instance = payload.get("instance") or payload.get("instanceName") or ""

    for _inst_hint, record in normalized:
        inbound = extract_inbound_content(record)
        if inbound is None:
            continue
        phone_norm = inbound.phone
        user_text = inbound.text

        if not permitted or phone_norm not in permitted:
            log.info("Telefone nao autorizado: %s", phone_norm)
            continue

        hint = _inst_hint if isinstance(_inst_hint, str) and _inst_hint.strip() else ""
        send_instance = (
            hint or str(webhook_instance) or default_inst or settings.evolution_instance
        ).strip()

        async with TypingSession(
            evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=send_instance,
            phone=phone_norm,
        ):
            messenger: StepMessenger | None = None
            if evo_base and evo_key and send_instance:
                messenger = StepMessenger(
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=send_instance,
                    phone=phone_norm,
                )

            inbound_resolved, audio_error = await resolve_inbound_audio_as_text(
                inbound,
                settings=settings,
                evo=evo,
                instance=send_instance,
            )
            if audio_error:
                reply_text = _truncate_whatsapp(polish_user_message(audio_error))
                if evo_base and evo_key and send_instance:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, user_text or "[audio]", reply_text)
                continue
            if inbound_resolved is not None:
                inbound = inbound_resolved
                user_text = inbound.text
            elif inbound.media and is_inbound_audio(
                inbound.media.mediatype, inbound.media.mimetype
            ):
                reply_text = _truncate_whatsapp(
                    polish_user_message(
                        "Nao trato mensagens de voz como arquivo. "
                        "Verifique a chave Gemini ou envie em texto."
                    )
                )
                if evo_base and evo_key and send_instance:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, "[audio]", reply_text)
                continue

            if not inbound.media and not is_placeholder_user_text(user_text or ""):
                pending_media_reply = await try_handle_pending_media_reply(
                    phone_norm,
                    user_text or "",
                    settings=settings,
                    http=http,
                    on_step=messenger.step if messenger else None,
                )
                if pending_media_reply is not None:
                    log.info(
                        "Resposta arquivo pendente phone=%s: %s",
                        phone_norm,
                        pending_media_reply[:120],
                    )
                    if messenger and messenger.sent_any:
                        await messenger.step(pending_media_reply)
                        record_exchange(phone_norm, user_text or "", messenger.combined())
                    else:
                        reply_text = _truncate_whatsapp(
                            polish_user_message(pending_media_reply)
                        )
                        if evo_base and evo_key and send_instance:
                            await pulse_whatsapp_typing()
                            await evo.send_text(
                                base_url=evo_base,
                                api_key=evo_key,
                                instance=send_instance,
                                number=phone_norm,
                                text=reply_text,
                            )
                            record_exchange(phone_norm, user_text or "", reply_text)
                    continue

            if inbound.media:
                if not is_storable_file_media(
                    inbound.media.mediatype, inbound.media.mimetype
                ):
                    log.warning(
                        "Midia nao-arquivavel no fluxo de ficheiros phone=%s tipo=%s",
                        phone_norm,
                        inbound.media.mediatype,
                    )
                    continue

                log.info(
                    "Midia recebida phone=%s tipo=%s arquivo=%s",
                    phone_norm,
                    inbound.media.mediatype,
                    inbound.media.filename[:80],
                )
                media_reply: str | None = None
                if media_has_explicit_intent(inbound):
                    media_reply = await route_explicit_inbound_media(
                        inbound,
                        settings=settings,
                        evo=evo,
                        http=http,
                        instance=send_instance,
                    )
                if not media_reply:
                    media_reply = await handle_ambiguous_inbound_media(
                        inbound,
                        settings=settings,
                        evo=evo,
                        instance=send_instance,
                    )
                if media_reply is not None:
                    if media_reply:
                        reply_text = _truncate_whatsapp(polish_user_message(media_reply))
                        if evo_base and evo_key and send_instance:
                            await pulse_whatsapp_typing()
                            await evo.send_text(
                                base_url=evo_base,
                                api_key=evo_key,
                                instance=send_instance,
                                number=phone_norm,
                                text=reply_text,
                            )
                            record_exchange(
                                phone_norm,
                                user_text or "[arquivo]",
                                reply_text,
                            )
                    else:
                        log.info(
                            "Midia aguardando lote phone=%s tipo=%s",
                            phone_norm,
                            inbound.media.mediatype,
                        )
                else:
                    log.error(
                        "Midia sem resposta phone=%s tipo=%s",
                        phone_norm,
                        inbound.media.mediatype,
                    )
                continue

            pending_reply = await try_handle_pending_password(
                phone_norm,
                user_text or "",
                ha=ha,
                catalog=catalog,
                message_record=inbound.record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            if pending_reply is not None:
                log.info(
                    "Resposta senha pendente phone=%s: %s",
                    phone_norm,
                    pending_reply[:120],
                )
                reply_text = _truncate_whatsapp(pending_reply)
                if evo_base and evo_key and send_instance:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, user_text or "", reply_text)
                continue

            send_instance_early = _resolve_evolution_instance(
                _inst_hint, webhook_instance, default_inst, settings
            )
            if await try_handle_password_vault_pending(
                phone_norm,
                user_text or "",
                record=inbound.record,
                settings=settings,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance_early,
            ):
                log.info("Cofre de senhas (pendente) phone=%s", phone_norm)
                continue

            if await try_handle_vault_intent_direct(
                phone_norm,
                user_text or "",
                settings=settings,
                record=inbound.record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance_early,
            ):
                log.info("Cofre de senhas (direto) phone=%s", phone_norm)
                continue

            registry_list_reply = try_personal_registry_list_reply(
                phone_norm, user_text or ""
            )
            if registry_list_reply:
                reply_text = _truncate_whatsapp(registry_list_reply)
                if evo_base and evo_key and send_instance_early:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance_early,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, user_text or "", reply_text)
                log.info("Listagem registro pessoal phone=%s", phone_norm)
                continue

            if await try_handle_portao_servico_inbound(
                phone_norm,
                user_text or "",
                ha=ha,
                catalog=catalog,
                evo=evo,
                evo_base=evo_base or "",
                evo_key=evo_key or "",
                instance=send_instance_early or "",
            ):
                log.info("Rotina portao servico tratou mensagem phone=%s", phone_norm)
                continue

            if await try_handle_portao_social_inbound(
                phone_norm,
                user_text or "",
                ha=ha,
                catalog=catalog,
                evo=evo,
                evo_base=evo_base or "",
                evo_key=evo_key or "",
                instance=send_instance_early or "",
                message_record=inbound.record,
            ):
                log.info("Rotina portao social tratou mensagem phone=%s", phone_norm)
                continue

            if (
                evo_base
                and evo_key
                and send_instance_early
                and http is not None
                and await try_handle_google_calendar_link_inbound(
                    phone_norm,
                    user_text or "",
                    http=http,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=send_instance_early,
                )
            ):
                log.info("Google Calendar link guardado phone=%s", phone_norm)
                continue

            if (
                evo_base
                and evo_key
                and send_instance_early
                and http is not None
                and await try_handle_instagram_link_pending(
                    phone_norm,
                    user_text or "",
                    settings=settings,
                    evo=evo,
                    http=http,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=send_instance_early,
                )
            ):
                log.info("Rotina Instagram (pending) phone=%s", phone_norm)
                record_exchange(phone_norm, user_text or "", "[fluxo Instagram]")
                continue

            ig_list_reply = try_instagram_links_list_reply(phone_norm, user_text or "")
            if ig_list_reply:
                reply_text = _truncate_whatsapp(ig_list_reply)
                if evo_base and evo_key and send_instance_early:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance_early,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, user_text or "", reply_text)
                log.info("Listagem Instagram phone=%s", phone_norm)
                continue

            ig_search_reply = try_search_instagram_profiles_reply(phone_norm, user_text or "")
            if ig_search_reply:
                reply_text = _truncate_whatsapp(ig_search_reply)
                if evo_base and evo_key and send_instance_early:
                    await pulse_whatsapp_typing()
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=send_instance_early,
                        number=phone_norm,
                        text=reply_text,
                    )
                    record_exchange(phone_norm, user_text or "", reply_text)
                log.info("Busca Instagram phone=%s", phone_norm)
                continue

            if (
                evo_base
                and evo_key
                and send_instance_early
                and http is not None
                and await try_handle_refresh_instagram_inbound(
                    phone_norm,
                    user_text or "",
                    settings=settings,
                    evo=evo,
                    http=http,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=send_instance_early,
                )
            ):
                log.info("Refresh Instagram phone=%s", phone_norm)
                record_exchange(phone_norm, user_text or "", "[atualizacao Instagram]")
                continue

            if (
                evo_base
                and evo_key
                and send_instance_early
                and http is not None
                and await try_handle_instagram_link_inbound(
                    phone_norm,
                    user_text or "",
                    settings=settings,
                    evo=evo,
                    http=http,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=send_instance_early,
                )
            ):
                log.info("Rotina Instagram (novo link) phone=%s", phone_norm)
                record_exchange(phone_norm, user_text or "", "[fluxo Instagram]")
                continue

            if is_instagram_link_pending(phone_norm):
                log.info("Instagram pending; ignorando Gemini phone=%s", phone_norm)
                continue

            await _process_inbound_message(
                inbound=inbound,
                _inst_hint=_inst_hint,
                webhook_instance=webhook_instance,
                default_inst=default_inst,
                settings=settings,
                ha=ha,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                catalog=catalog,
                cameras=cameras,
                http=http,
                send_instance_early=send_instance_early,
                gemini_cache_name=gemini_cache_name,
            )


async def _process_inbound_message(
    *,
    inbound: InboundContent,
    _inst_hint: str | None,
    webhook_instance: Any,
    default_inst: str,
    settings: AppSettings,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    catalog: DevicesCatalog,
    cameras: CamerasCatalog,
    http: httpx.AsyncClient | None,
    send_instance_early: str,
    gemini_cache_name: str | None = None,
) -> None:
        if inbound.media:
            log.error(
                "Midia nao deveria chegar ao Gemini phone=%s tipo=%s",
                inbound.phone,
                inbound.media.mediatype,
            )
            return

        phone_norm = inbound.phone
        user_text = inbound.text

        hint = _inst_hint if isinstance(_inst_hint, str) and _inst_hint.strip() else ""
        send_instance = (
            hint or str(webhook_instance) or default_inst or settings.evolution_instance
        ).strip()

        store = get_store(phone_norm)
        timings = MessageTimings(phone=phone_norm)

        assistant, memory_context, memory_in_cache = build_gemini_assistant_for_user(
            settings,
            catalog,
            cameras,
            store,
            catalog_cache_name=gemini_cache_name,
        )

        ha_t0 = time.monotonic()
        states, states_map = await fetch_catalog_entity_states(ha, catalog)
        timings.mark_ha_done((time.monotonic() - ha_t0) * 1000.0)
        ctx, total = build_entities_context(states)

        log.info(
            "Mensagem de %s: %s (contexto chars=%s, entidades catalogo=%s)",
            phone_norm,
            user_text[:120] if user_text else "",
            len(ctx),
            total,
        )

        history_entries = get_recent(phone_norm)
        history_text = format_for_prompt(history_entries)
        gemini_user_message = augment_user_message_for_affirmative(
            user_text or "", history_entries
        )
        scheduled_block = format_pending_for_prompt(get_scheduled_store(phone_norm).list_pending())
        if scheduled_block:
            gemini_user_message = f"{scheduled_block}\n\n{gemini_user_message}"
        log.info(
            "Historico phone=%s mensagens=%s chars=%s",
            phone_norm,
            len(history_entries),
            len(history_text),
        )

        messenger: StepMessenger | None = None
        if evo_base and evo_key and send_instance:
            messenger = StepMessenger(
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
                phone=phone_norm,
            )

        async def _gemini_decide(**kwargs: Any) -> dict[str, Any]:
            t0 = time.monotonic()
            result = await asyncio.to_thread(assistant.decide, **kwargs)
            timings.add_gemini((time.monotonic() - t0) * 1000.0)
            return result

        decision = await _gemini_decide(
            user_message=gemini_user_message,
            entities_context=ctx,
            conversation_history=history_text,
            user_memory_context=memory_context,
            memory_in_cache=memory_in_cache,
        )
        decision = correct_affirmative_misroute(
            decision,
            user_text=user_text or "",
            history_entries=history_entries,
            catalog=catalog,
        )
        decision, retry_scenario_id = normalize_gemini_action(decision, catalog)
        active_scenario_id = retry_scenario_id
        retries_used = 0

        if needs_confirmation_execution_retry(
            user_text or "", history_entries, decision
        ) and retries_used < _GEMINI_MAX_RETRIES:
            retry_msg = (
                f"{gemini_user_message}\n\n"
                f"{confirmation_execution_retry_message(last_assistant_text(history_entries))}"
            )
            log.info("Retry Gemini: confirmacao sem call_service phone=%s", phone_norm)
            retries_used += 1
            decision = await _gemini_decide(
                user_message=retry_msg,
                entities_context=ctx,
                conversation_history=history_text,
                user_memory_context=memory_context,
                memory_in_cache=memory_in_cache,
            )
            decision = correct_affirmative_misroute(
                decision,
                user_text=user_text or "",
                history_entries=history_entries,
                catalog=catalog,
            )
            decision, retry_scenario_id = normalize_gemini_action(decision, catalog)
            if retry_scenario_id:
                active_scenario_id = retry_scenario_id

        if retry_scenario_id and retries_used < _GEMINI_MAX_RETRIES:
            ctx_retry = await _apply_scenario_context_for_retry(
                ha, catalog, ctx, retry_scenario_id, states_map=states_map
            )
            log.info(
                "Retry Gemini com estados HA phone=%s cenario=%s",
                phone_norm,
                retry_scenario_id,
            )
            retries_used += 1
            decision = await _gemini_decide(
                user_message=gemini_user_message,
                entities_context=ctx_retry,
                conversation_history=history_text,
                user_memory_context=memory_context,
                memory_in_cache=memory_in_cache,
            )
            decision = correct_affirmative_misroute(
                decision,
                user_text=user_text or "",
                history_entries=history_entries,
                catalog=catalog,
            )
            decision, retry_scenario_id = normalize_gemini_action(decision, catalog)
            if retry_scenario_id:
                active_scenario_id = retry_scenario_id

        decision = try_fact_check_decision_override(
            decision,
            user_text=user_text or "",
            settings=settings,
        )
        decision = try_memory_delete_override(
            decision,
            phone=phone_norm,
            user_text=user_text or "",
            history_text=history_text,
            history_entries=history_entries,
        )
        decision = try_google_calendar_decision_override(
            decision,
            phone=phone_norm,
            user_text=user_text or "",
        )

        if not _decision_is_complete(decision):
            reply_preview = str(decision.get("response") or "").strip()
            needs_retry = (
                _is_placeholder_scenario_response(reply_preview)
                or is_internal_instruction_leak(reply_preview)
                or (_normalize_action_name(decision.get("action")) not in VALID_GEMINI_ACTIONS)
                or (
                    _normalize_action_name(decision.get("action")) == "reply"
                    and not reply_preview
                )
            )
            if needs_retry and active_scenario_id and retries_used < _GEMINI_MAX_RETRIES:
                ctx_retry = await _apply_scenario_context_for_retry(
                    ha, catalog, ctx, active_scenario_id, states_map=states_map
                )
                log.info(
                    "Segundo retry Gemini phone=%s cenario=%s",
                    phone_norm,
                    active_scenario_id,
                )
                retries_used += 1
                decision = await _gemini_decide(
                    user_message=gemini_user_message,
                    entities_context=ctx_retry,
                    conversation_history=history_text,
                    user_memory_context=memory_context,
                    memory_in_cache=memory_in_cache,
                )
                decision = correct_affirmative_misroute(
                    decision,
                    user_text=user_text or "",
                    history_entries=history_entries,
                    catalog=catalog,
                )
                decision, _ = normalize_gemini_action(decision, catalog)

        decision_list = split_gemini_decisions(decision)
        if len(decision_list) > 1:
            _log_gemini_decision(phone_norm, decision)
            reply_text = await execute_decision_batch(
                decision_list,
                phone=phone_norm,
                messenger=messenger,
                ha=ha,
                catalog=catalog,
                user_text=user_text or "",
                message_record=inbound.record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
                settings=settings,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=reply_text,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        decision = decision_list[0]
        decision = await _ensure_user_friendly_decision(
            decision,
            ha=ha,
            catalog=catalog,
            scenario_id=active_scenario_id,
            states_map=states_map,
        )
        _log_gemini_decision(phone_norm, decision)

        action = _normalize_action_name(decision.get("action"))
        if action == "search_photos" and http is not None and send_instance:
            await handle_search_photos(
                decision,
                settings=settings,
                evo=evo,
                http=http,
                phone=phone_norm,
                instance=send_instance,
                messenger=messenger,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=str(decision.get("response") or "").strip() or "Fotos enviadas.",
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "get_camera_snapshot" and http is not None and send_instance:
            await handle_get_camera_snapshot(
                decision,
                settings=settings,
                cameras=cameras,
                evo=evo,
                http=http,
                phone=phone_norm,
                instance=send_instance,
                messenger=messenger,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=str(decision.get("response") or "").strip() or "Imagem da camera enviada.",
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "delete_from_memory":
            del_reply = handle_delete_from_memory(decision, phone_norm)
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=del_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "send_user_file" and send_instance:
            file_reply = await handle_send_user_file(
                decision,
                settings=settings,
                evo=evo,
                phone=phone_norm,
                instance=send_instance,
                messenger=messenger,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=file_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "send_instagram_link" and send_instance:
            ig_reply = await handle_send_instagram_link(
                decision,
                settings=settings,
                evo=evo,
                phone=phone_norm,
                instance=send_instance,
                messenger=messenger,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=ig_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "list_instagram_links":
            list_reply = handle_list_instagram_links(phone_norm)
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=list_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "search_instagram_links":
            search_reply = handle_search_instagram_links(decision, phone_norm)
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=search_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "refresh_instagram_link" and send_instance and http is not None:
            refresh_reply = await handle_refresh_instagram_link(
                decision,
                settings=settings,
                evo=evo,
                http=http,
                phone=phone_norm,
                instance=send_instance,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=refresh_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "delete_instagram_link":
            del_ig = handle_delete_instagram_link(decision, phone_norm)
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=del_ig,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "fact_check_claim" and http is not None:
            fc_reply = await handle_fact_check_claim(
                decision,
                settings=settings,
                http=http,
                messenger=messenger,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=fc_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        if action == "google_calendar_list_events" and http is not None:
            cal_reply = await handle_google_calendar_list_events(
                decision,
                phone=phone_norm,
                http=http,
            )
            await _finish_whatsapp_exchange(
                phone=phone_norm,
                user_text=user_text or "",
                messenger=messenger,
                reply_text=cal_reply,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=send_instance,
            )
            _log_message_timings(timings, messenger)
            return

        reply_text = await execute_decision(
            decision,
            ha=ha,
            catalog=catalog,
            phone=phone_norm,
            user_text=user_text or "",
            entities_context_used=True,
            messenger=messenger,
            message_record=inbound.record,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=send_instance,
            settings=settings,
        )

        if not phone_norm:
            _log_message_timings(timings, messenger)
            return
        if (
            not reply_text.strip()
            and not (messenger and messenger.sent_any)
        ):
            _log_message_timings(timings, messenger)
            return
        if not evo_base or not evo_key or not send_instance:
            log.error(
                "Envio Evolution bloqueado: base_ok=%s key_ok=%s inst=%s",
                bool(evo_base),
                bool(evo_key),
                repr(send_instance),
            )
            _log_message_timings(timings, messenger)
            return

        await _finish_whatsapp_exchange(
            phone=phone_norm,
            user_text=user_text or "",
            messenger=messenger,
            reply_text=reply_text,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=send_instance,
        )
        _log_message_timings(timings, messenger)
