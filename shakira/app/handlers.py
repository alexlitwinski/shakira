"""Processamento de webhooks Evolution e fluxo Gemini + Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.confirmation_context import (
    augment_user_message_for_affirmative,
    correct_affirmative_misroute,
)
from app.conversation_history import format_for_prompt, get_recent, record_exchange
from app.devices_catalog import DevicesCatalog
from app.frigate import FrigateClient, FrigateError
from app.evolution import EvolutionClient
from app.gemini import GeminiAssistant
from app.gemini_cache import ensure_catalog_cache
from app.homeassistant import HomeAssistantClient
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
    prepend_scenario_states_to_context,
)
from app.user_memory import InboundContent, InboundMedia, UserMemoryStore, get_store
from app.user_memory_cache import ensure_user_memory_cache, invalidate_user_memory_cache
from app.user_memory_prompts import USER_MEMORY_ACTIONS_INSTRUCTION
from app.user_memory_actions import (
    handle_delete_from_memory,
    try_memory_delete_override,
)
from app.pending_media import (
    build_media_choice_prompt,
    build_pending_clarification,
    build_pending_processing_wait,
    build_pending_progress_message,
    build_personal_description_prompt,
    classify_explicit_media_intent,
    classify_pending_reply,
    download_inbound_media_bytes,
    extract_album_name,
    extract_personal_description,
    is_placeholder_user_text,
    media_has_explicit_intent,
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

ENTITY_PERMITTED = "input_text.whatsapp_bot_permitidos"

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
    }
)

_USER_MEMORY_CACHE_MIN_CHARS = int(os.environ.get("SHAKIRA_USER_MEMORY_CACHE_MIN_CHARS", "6000"))

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


def normalize_phone_digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def parse_allowed_numbers(raw: str) -> set[str]:
    if not raw:
        return set()
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    out: set[str] = set()
    for p in parts:
        if not p:
            continue
        d = normalize_phone_digits(p)
        if d:
            out.add(d)
    return out


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


def _accept_inbound_once(phone: str, text: str) -> bool:
    key = f"{phone}:{text}"
    now = time.monotonic()
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

    if not _accept_inbound_once(digits, text or "[media]"):
        log.info("Ignorando mensagem duplicada phone=%s", digits)
        return None

    if not text and media:
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


def build_entities_context(states: list[dict[str, Any]]) -> tuple[str, int]:
    max_chars = int(os.environ.get("ENTITY_CONTEXT_MAX_CHARS", "120000"))
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
    total = len(states)
    if len(body) <= max_chars:
        return body + f"\n\nTotal de entidades: {total}", total
    truncated = body[:max_chars].rsplit("\n", 1)[0]
    note = f"\n\n[Contexto truncado] Mostrando apenas parte das {total} entidades."
    return truncated + note, total


async def fetch_permitted_phones_raw(ha: HomeAssistantClient) -> str:
    s = await ha.get_state(ENTITY_PERMITTED)
    if s and isinstance(s.get("state"), str):
        return s["state"]
    return ""


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
    if messenger and messenger.sent_any:
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


def extract_target_entity_id(
    domain: str,
    service: str,
    service_data: dict[str, Any] | None,
    decision_entity_id: Any = None,
) -> str | None:
    if isinstance(decision_entity_id, str) and decision_entity_id.strip():
        return decision_entity_id.strip()
    if not service_data:
        return None
    eid = service_data.get("entity_id")
    if isinstance(eid, str) and eid.strip():
        return eid.strip()
    if isinstance(eid, list) and eid:
        return str(eid[0]).strip()
    return None


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


def normalize_gemini_action(
    decision: dict[str, Any], catalog: DevicesCatalog
) -> tuple[dict[str, Any], str | None]:
    """Corrige action invalida (ex.: id de cenario). Retorna (decision, scenario_id para retry)."""
    action = str(decision.get("action") or "reply").lower()
    if action in VALID_GEMINI_ACTIONS:
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
) -> str:
    ctx = await prepend_scenario_states_to_context(ha, catalog, ctx, scenario_id)
    correction = build_gemini_scenario_correction_block(scenario_id)
    return f"{correction}\n\n{ctx}"


async def _ensure_user_friendly_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    scenario_id: str | None,
) -> dict[str, Any]:
    """Garante resposta amigavel; nunca envia instrucao interna ou dump tecnico."""
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
        friendly = await build_friendly_reply_from_scenario(ha, catalog, scenario_id)
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
    out["response"] = "Nao consegui concluir a resposta agora. Tente de novo em instantes."
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
    data: dict[str, Any] = {"entity_id": entity_id}
    if not raw:
        return data
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
        return "Nao foi possivel destrancar a porta. Tente novamente em instantes."


async def try_handle_pending_password(
    phone: str,
    user_text: str,
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
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
    return await _execute_unlock_pending(phone, pending, ha=ha)


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
    fallback = catalog.build_catalog_context()
    if cameras.cameras:
        fallback = f"{fallback}\n\n{cameras.build_catalog_context()}"
    return GeminiAssistant(
        settings.gemini_api_key,
        model=model_name,
        cache_name=cache_name,
        catalog_fallback=fallback,
    )


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
    memory_in_cache = False

    catalog_fallback = catalog.build_catalog_context()
    if cameras.cameras:
        catalog_fallback = f"{catalog_fallback}\n\n{cameras.build_catalog_context()}"
    catalog_fallback = f"{USER_MEMORY_ACTIONS_INSTRUCTION}\n\n{catalog_fallback}"

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
        )
        if user_cache:
            memory_in_cache = True
            assistant = GeminiAssistant(
                settings.gemini_api_key,
                model=model_name,
                cache_name=user_cache,
                catalog_fallback=catalog_fallback,
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
) -> str:
    raw = path.read_bytes()
    entry = store.save_file(
        raw,
        filename=pending.filename,
        mime_type=pending.mime_type,
        label=description[:120],
        caption=pending.caption,
    )
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
    if not settings.photoprism_url or not settings.photoprism_token:
        return (
            "PhotoPrism nao esta configurado no add-on. "
            "Defina photoprism_url e photoprism_token nas opcoes."
        )
    pp = PhotoprismClient(
        http,
        base_url=settings.photoprism_url,
        token=settings.photoprism_token,
        api_prefix=settings.photoprism_api_prefix,
    )
    try:
        result = await pp.upload_photo(
            file_bytes=raw,
            filename=filename,
            album=album,
            mime_type=mime_type,
            supervisor_token=settings.supervisor_token,
        )
    except PhotoprismAuthError:
        return "Erro de autenticacao no PhotoPrism. Verifique o token nas opcoes."
    except PhotoprismError as e:
        log.warning("Upload PhotoPrism falhou: %s", e)
        return f"Nao consegui enviar ao PhotoPrism: {e}"

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
        return "O usuario enviou um arquivo, mas nao foi possivel baixa-lo para guardar."

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
        return f"[Sistema] Nao foi possivel guardar o arquivo: {e}"
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
    hit = store.get_pending_file()
    if not hit:
        return None

    pending, path = hit

    if pending.stage == "processing":
        return build_pending_processing_wait()

    if pending.stage == "description":
        if any(w in user_text.casefold() for w in ("cancela", "cancelar", "esquece", "descarta", "deixa")):
            store.clear_pending_file()
            return "Ok, descartei o arquivo."
        description = extract_personal_description(user_text, pending.caption)
        if not description:
            return build_personal_description_prompt()
        store.set_pending_stage("processing")
        if on_step:
            await on_step(build_pending_progress_message("personal"))
        try:
            return _save_pending_bytes_to_personal(store, pending, path, description)
        except ValueError as e:
            store.clear_pending_file()
            return f"Nao foi possivel guardar: {e}"
        except OSError:
            store.clear_pending_file()
            return "Erro ao gravar o arquivo."

    choice = classify_pending_reply(user_text, is_image=pending.is_image)

    if choice == "cancel":
        store.clear_pending_file()
        return "Ok, descartei o arquivo."

    if choice == "unknown":
        return build_pending_clarification(is_image=pending.is_image)

    if choice == "photoprism":
        if not pending.is_image:
            return (
                "So posso enviar *fotos* ao PhotoPrism. "
                "Para este arquivo, responda *pessoal* para guardar na memoria."
            )
        if http is None:
            return "Servico temporariamente indisponivel para PhotoPrism."
        album = extract_album_name(user_text)
        store.set_pending_stage("processing")
        if on_step:
            await on_step(build_pending_progress_message("photoprism", album=album))
        raw = path.read_bytes()
        msg = await _upload_bytes_to_photoprism(
            raw,
            filename=pending.filename,
            album=album,
            mime_type=pending.mime_type,
            settings=settings,
            http=http,
        )
        store.clear_pending_file()
        return msg

    label = extract_personal_description(user_text, pending.caption)
    if not label:
        store.set_pending_stage("description")
        return build_personal_description_prompt()

    store.set_pending_stage("processing")
    if on_step:
        await on_step(build_pending_progress_message("personal"))
    raw = path.read_bytes()
    try:
        return _save_pending_bytes_to_personal(store, pending, path, label)
    except ValueError as e:
        store.clear_pending_file()
        return f"Nao foi possivel guardar: {e}"
    except OSError:
        store.clear_pending_file()
        return "Erro ao gravar o arquivo."


async def handle_ambiguous_inbound_media(
    inbound: InboundContent,
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    instance: str,
) -> str | None:
    """
    Arquivo sem instrucao: baixa, guarda como pendente e retorna mensagem para o usuario.
    None se nao aplicavel.
    """
    if not inbound.media:
        return None
    if media_has_explicit_intent(inbound):
        return None

    downloaded = await download_inbound_media_bytes(
        inbound, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return "Recebi seu arquivo, mas nao consegui baixa-lo. Tente enviar de novo."

    raw, mimetype, fname = downloaded
    media = inbound.media
    store = get_store(inbound.phone)
    try:
        pending = store.save_pending_file(
            raw,
            filename=fname or media.filename,
            mime_type=mimetype or media.mimetype,
            mediatype=media.mediatype,
            caption=media.caption,
        )
    except ValueError as e:
        return f"Nao foi possivel receber o arquivo: {e}"

    return build_media_choice_prompt(is_image=pending.is_image, filename=pending.filename)


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

    downloaded = await download_inbound_media_bytes(
        inbound, settings=settings, evo=evo, instance=instance
    )
    if not downloaded:
        return "Nao consegui baixar o arquivo para processar seu pedido."

    raw, mimetype, fname = downloaded
    media = inbound.media
    intent = classify_explicit_media_intent(inbound)

    store = get_store(inbound.phone)

    if intent == "photoprism":
        if media.mediatype != "image" and not (mimetype or "").startswith("image/"):
            return (
                "PhotoPrism aceita fotos. Para este arquivo use memoria pessoal "
                '(responda "guardar" na legenda).'
            )
        if http is None:
            return "Servico indisponivel para envio ao PhotoPrism."
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
            return f"Nao foi possivel receber o arquivo: {e}"
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
        msg = intro or "Nao encontrei esse arquivo na sua memoria."
        if not hit and not intro:
            files = store.list_files()
            if files:
                names = ", ".join(f"{f.filename} (id={f.id})" for f in files[-5:])
                msg = f"Nao achei o arquivo. Os ultimos guardados: {names}."
        await say(msg)
        return msg

    meta, path = hit
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        return "Evolution nao configurado para enviar o arquivo."

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
        msg = "Encontrei o arquivo mas nao consegui enviar pelo WhatsApp."
        await say(msg)
        return msg
    return intro or f"Enviei o arquivo {meta.filename}."


def handle_save_memory(decision: dict[str, Any], phone: str) -> str:
    text = str(decision.get("memory_text") or "").strip()
    if not text:
        reply = str(decision.get("response") or "").strip()
        return reply or "Nao entendi o que devo guardar na memoria."
    label = str(decision.get("memory_label") or "").strip()
    store = get_store(phone)
    store.add_memory(text, label=label)
    invalidate_user_memory_cache(store)
    confirm = str(decision.get("response") or "").strip()
    if confirm:
        return confirm
    short = text if len(text) <= 80 else text[:77] + "..."
    return f"Guardei na sua memoria: {short}"


async def execute_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    phone: str,
    user_text: str,
    entities_context_used: bool,
    messenger: StepMessenger | None = None,
) -> str:
    action = str(decision.get("action") or "reply").lower()
    if action not in VALID_GEMINI_ACTIONS:
        log.warning("execute_decision: acao '%s' invalida; tratando como reply", action)
        action = "reply"
    reply = str(decision.get("response") or "").strip()
    log.info("execute_decision phone=%s action=%s steps=%s", phone, action, bool(messenger))

    async def deliver(text: str) -> None:
        t = polish_user_message(text)
        if t and messenger:
            await messenger.step(t)

    async def finalize(extra: str) -> str:
        base = polish_user_message(reply)
        extra_clean = polish_user_message(extra)
        msg = (base + ("\n\n" + extra_clean if extra_clean else "")).strip()
        out = msg or extra_clean or "Feito."
        if messenger:
            if out:
                await messenger.step(out)
            return ""
        return out

    if (
        messenger
        and reply
        and not _is_placeholder_scenario_response(reply)
        and action not in ("reply", "list_entities")
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
            return await finalize("Nao entendi qual aparelho devo verificar.")
        eid = eid.strip()
        await deliver(format_checking(eid, catalog))
        st = await ha.get_state(eid)
        if not st:
            return await finalize("Nao encontrei esse aparelho na casa.")
        result = format_state_value(eid, st, catalog)
        if messenger:
            await deliver(result)
            return ""
        return await finalize(result)

    if action == "call_service":
        domain = decision.get("domain")
        service = decision.get("service")
        svc_data = decision.get("service_data")
        if not isinstance(domain, str) or not isinstance(service, str):
            return await finalize("Comando incompleto (domain/service).")
        if svc_data is not None and not isinstance(svc_data, dict):
            return await finalize("service_data invalido.")

        domain = domain.strip()
        service = service.strip()
        raw_svc_data = dict(svc_data) if isinstance(svc_data, dict) else {}

        target = extract_target_entity_id(domain, service, raw_svc_data, decision.get("entity_id"))
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
                "Ainda nao tenho nenhum aparelho autorizado para controlar por aqui."
            )
        if target not in allowed:
            return await finalize(
                "Nao tenho permissao para alterar esse aparelho. "
                "So posso controlar o que esta na lista de dispositivos do assistente."
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
            if messenger:
                await deliver(done)
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
            "Pedido de fotos recebido. Se nada chegar, verifique PhotoPrism nas opcoes do add-on."
        )

    if action == "get_camera_snapshot":
        return await finalize(
            "Pedido de camera recebido. Se nada chegar, verifique Frigate nas opcoes do add-on."
        )

    if action == "save_memory":
        result = handle_save_memory(decision, phone)
        return await finalize(result)

    if action == "send_user_file":
        return await finalize(
            "Arquivo solicitado. Se nada chegar, verifique se o id ou nome estao corretos."
        )

    if action == "delete_from_memory":
        result = handle_delete_from_memory(decision, phone)
        return await finalize(result)

    fallback = reply or "Nao entendi o proximo passo."
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
    """Obtem snapshot do Frigate e envia pelo WhatsApp."""
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        log.error("Envio de camera bloqueado: Evolution nao configurado")
        return

    intro = str(decision.get("response") or "").strip()
    raw_id = decision.get("camera_id")
    camera_id = cameras.resolve_camera_id(str(raw_id) if raw_id is not None else None)

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

    if not settings.frigate_url:
        msg = (
            intro + "\n\nFrigate nao configurado. Defina frigate_url nas opcoes do add-on."
        ).strip()
        await say(msg)
        return

    if not cameras.cameras:
        msg = (
            intro
            + "\n\nNenhuma camera configurada. Crie /config/shakira_cameras.yaml."
        ).strip()
        await say(msg)
        return

    if not camera_id:
        known = ", ".join(f"{c.id} ({c.name})" for c in cameras.cameras[:8])
        msg = (
            intro + f"\n\nNao identifiquei a camera. Cameras disponiveis: {known}."
        ).strip()
        await say(msg)
        return

    cam = cameras.camera_map().get(camera_id)
    frigate = FrigateClient(http, base_url=settings.frigate_url)
    log.info("Frigate snapshot camera=%s url=%s", camera_id, settings.frigate_url)

    label = cam.name if cam else camera_id
    if intro:
        await say(intro)
    await say(f"Vou buscar a imagem da camera {label}...")

    try:
        image_bytes = await frigate.get_latest_snapshot(camera_id)
    except FrigateError as e:
        log.error("Frigate falhou: %s", e, exc_info=True)
        await say(f"Nao consegui obter a imagem: {e}")
        return

    caption = f"Camera: {label}"[:1024]
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
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text="Capturei a imagem mas nao consegui enviar pelo WhatsApp.",
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
            intro + "\n\nPhotoPrism nao configurado. "
            "Defina photoprism_url e photoprism_token nas opcoes do add-on."
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
        await say("Erro de autenticacao no PhotoPrism. Verifique o token.")
        return
    except PhotoprismError as e:
        log.error("PhotoPrism falhou: %s", e, exc_info=True)
        extra = ""
        diag = getattr(e, "diagnostic", None) or {}
        if isinstance(diag, dict) and diag.get("hints"):
            extra = f" {diag['hints'][0]}"
        await say(f"Nao consegui buscar fotos: {e}{extra}")
        return

    if not photos:
        detail = ""
        if city:
            detail = f" (pessoa + {city})"
        await say(f"Nao encontrei fotos com esses criterios{detail}.")
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
            text="Encontrei fotos mas nao consegui enviar as imagens.",
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
) -> None:
    gemini_key = settings.gemini_api_key.strip()
    if not gemini_key:
        log.warning("Chave Gemini ausente nas opcoes do add-on")
        return

    catalog = DevicesCatalog.load(settings.devices_config_path)
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
            if not inbound.media and not is_placeholder_user_text(user_text or ""):
                messenger: StepMessenger | None = None
                if evo_base and evo_key and send_instance:
                    messenger = StepMessenger(
                        evo=evo,
                        evo_base=evo_base,
                        evo_key=evo_key,
                        instance=send_instance,
                        phone=phone_norm,
                    )
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
        assistant, memory_context, memory_in_cache = build_gemini_assistant_for_user(
            settings,
            catalog,
            cameras,
            store,
            catalog_cache_name=gemini_cache_name,
        )

        states = await ha.get_states()
        ctx, total = build_entities_context(states)

        log.info(
            "Mensagem de %s: %s (contexto chars=%s, total entidades HA=%s)",
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

        decision = await asyncio.to_thread(
            assistant.decide,
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

        if retry_scenario_id:
            ctx_retry = await _apply_scenario_context_for_retry(
                ha, catalog, ctx, retry_scenario_id
            )
            log.info(
                "Retry Gemini com estados HA phone=%s cenario=%s",
                phone_norm,
                retry_scenario_id,
            )
            decision = await asyncio.to_thread(
                assistant.decide,
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

        decision = try_memory_delete_override(
            decision,
            phone=phone_norm,
            user_text=user_text or "",
            history_text=history_text,
            history_entries=history_entries,
        )

        reply_preview = str(decision.get("response") or "").strip()
        needs_retry = (
            _is_placeholder_scenario_response(reply_preview)
            or is_internal_instruction_leak(reply_preview)
            or (str(decision.get("action") or "reply").lower() not in VALID_GEMINI_ACTIONS)
            or (str(decision.get("action") or "reply").lower() == "reply" and not reply_preview)
        )
        if needs_retry and active_scenario_id:
            ctx_retry = await _apply_scenario_context_for_retry(
                ha, catalog, ctx, active_scenario_id
            )
            log.info(
                "Segundo retry Gemini phone=%s cenario=%s",
                phone_norm,
                active_scenario_id,
            )
            decision = await asyncio.to_thread(
                assistant.decide,
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

        decision = await _ensure_user_friendly_decision(
            decision,
            ha=ha,
            catalog=catalog,
            scenario_id=active_scenario_id,
        )

        _log_gemini_decision(phone_norm, decision)

        action = str(decision.get("action") or "reply").lower()
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
            return

        reply_text = await execute_decision(
            decision,
            ha=ha,
            catalog=catalog,
            phone=phone_norm,
            user_text=user_text or "",
            entities_context_used=True,
            messenger=messenger,
        )

        if not phone_norm:
            return
        if (
            not reply_text.strip()
            and not (messenger and messenger.sent_any)
        ):
            return
        if not evo_base or not evo_key or not send_instance:
            log.error(
                "Envio Evolution bloqueado: base_ok=%s key_ok=%s inst=%s",
                bool(evo_base),
                bool(evo_key),
                repr(send_instance),
            )
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
