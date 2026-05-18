"""Processamento de webhooks Evolution e fluxo Gemini + Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from app.config import AppSettings
from app.evolution import EvolutionClient
from app.gemini import GeminiAssistant
from app.homeassistant import HomeAssistantClient

log = logging.getLogger(__name__)

ENTITY_PERMITTED = "input_text.whatsapp_bot_permitidos"


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
    """Retorna lista (instance_opcional, registro_msg)."""

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


def extract_text_and_sender(record: dict[str, Any]) -> tuple[str | None, str | None]:
    key = record.get("key") or {}
    remote = key.get("remoteJid") or record.get("remoteJid") or ""
    if not remote:
        return None, None
    if remote.endswith("@g.us"):
        log.debug("Ignorando grupo: %s", remote)
        return None, None

    digits = normalize_phone_digits(remote.split("@")[0])

    msg = record.get("message") or {}
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
    if key.get("fromMe"):
        return None, None
    return digits, text


def build_entities_context(states: list[dict[str, Any]]) -> tuple[str, int]:
    """Limita tamanho do contexto por seguranca/memoria."""

    max_chars = int(os.environ.get("ENTITY_CONTEXT_MAX_CHARS", "120000"))
    lines: list[str] = []
    for s in sorted(states, key=lambda x: x.get("entity_id", "")):
        eid = s.get("entity_id", "")
        st = str(s.get("state", ""))
        name = ""
        attrs = s.get("attributes") or {}
        if isinstance(attrs, dict):
            name = str(attrs.get("friendly_name") or "")
        lines.append(f"{eid}\t{st}\t{name}")
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
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def execute_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    entities_context_used: bool,
    _states_snapshot: list[dict[str, Any]],
) -> str:
    action = str(decision.get("action") or "reply").lower()
    reply = str(decision.get("response") or "").strip()

    async def finalize(extra: str) -> str:
        base = reply or ""
        msg = (base + ("\n\n" + extra if extra else "")).strip()
        return msg or extra or "Feito."

    if action in ("reply", "list_entities"):
        if not reply and entities_context_used:
            return "Ok."
        return await finalize("")

    if action == "get_state":
        eid = decision.get("entity_id")
        if not isinstance(eid, str) or not eid.strip():
            return await finalize("Informe uma entidade valida.")
        st = await ha.get_state(eid.strip())
        if not st:
            return await finalize(f"Entidade nao encontrada: {eid}")
        fname = ""
        attrs = st.get("attributes") or {}
        if isinstance(attrs, dict):
            fname = str(attrs.get("friendly_name") or "")
        extra = f"{eid} -> {st.get('state')} ({fname})".strip()
        return await finalize(extra)

    if action == "call_service":
        domain = decision.get("domain")
        service = decision.get("service")
        svc_data = decision.get("service_data")
        if not isinstance(domain, str) or not isinstance(service, str):
            return await finalize("Comando incompleto (domain/service).")
        if svc_data is not None and not isinstance(svc_data, dict):
            return await finalize("service_data invalido.")
        try:
            result = await ha.call_service(
                domain.strip(),
                service.strip(),
                svc_data if isinstance(svc_data, dict) else None,
            )
            extra = ""
            if isinstance(result, dict) and result.get("service_response"):
                extra = str(result["service_response"])[:1500]
            elif result not in (None, "", []):
                extra = str(result)[:1500]
            return await finalize(extra if extra else "")
        except httpx.HTTPStatusError as e:
            log.warning("Erro HA service: %s", e.response.text[:500])
            return await finalize(f"Erro ao executar: {e.response.status_code}")

    return reply or "Nao entendi o proximo passo."


async def handle_evolution_payload(
    payload: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    evo: EvolutionClient,
    settings: AppSettings,
) -> None:
    gemini_key = settings.gemini_api_key.strip()
    if not gemini_key:
        log.warning("Chave Gemini ausente nas opcoes do add-on")
        return

    permitted_raw = await fetch_permitted_phones_raw(ha)
    permitted = parse_allowed_numbers(permitted_raw)
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    default_inst = settings.evolution_instance.strip()

    if not permitted:
        log.warning("Nenhum numero permitido em %s", ENTITY_PERMITTED)
    if not evo_base or not evo_key:
        log.warning("Evolution URL ou api key ausentes nas opcoes do add-on")

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    assistant = GeminiAssistant(gemini_key, model=model_name)

    normalized = normalize_evolution_payload(payload)
    webhook_instance = payload.get("instance") or payload.get("instanceName") or ""

    for _inst_hint, record in normalized:
        pair = extract_text_and_sender(record)
        phone, user_text = pair
        if phone is None:
            continue
        phone_norm = normalize_phone_digits(phone)

        if not permitted or phone_norm not in permitted:
            log.info("Telefone nao autorizado: %s", phone_norm)
            continue

        states = await ha.get_states()
        ctx, total = build_entities_context(states)

        log.info(
            "Mensagem de %s: %s (contexto chars=%s, total entidades HA=%s)",
            phone_norm,
            user_text[:120] if user_text else "",
            len(ctx),
            total,
        )

        decision = await asyncio.to_thread(
            assistant.decide,
            user_message=user_text or "",
            entities_context=ctx,
        )

        reply_text = await execute_decision(
            decision,
            ha=ha,
            entities_context_used=True,
            _states_snapshot=states,
        )

        reply_text = _truncate_whatsapp(reply_text)

        hint = _inst_hint if isinstance(_inst_hint, str) and _inst_hint.strip() else ""
        send_instance = (hint or str(webhook_instance) or default_inst or settings.evolution_instance).strip()

        if not phone_norm or not reply_text.strip():
            continue
        if not evo_base or not evo_key or not send_instance:
            log.error(
                "Envio Evolution bloqueado: base_ok=%s key_ok=%s inst=%s",
                bool(evo_base),
                bool(evo_key),
                repr(send_instance),
            )
            continue

        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=send_instance,
            number=phone_norm,
            text=reply_text,
        )
