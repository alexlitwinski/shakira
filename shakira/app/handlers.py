"""Processamento de webhooks Evolution e fluxo Gemini + Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import AppSettings
from app.conversation_history import format_for_prompt, get_recent, record_exchange
from app.devices_catalog import DevicesCatalog
from app.evolution import EvolutionClient
from app.gemini import GeminiAssistant
from app.gemini_cache import ensure_catalog_cache
from app.homeassistant import HomeAssistantClient
from app.photoprism import (
    PhotoprismAuthError,
    PhotoprismClient,
    PhotoprismError,
    PhotoResult,
)

log = logging.getLogger(__name__)

ENTITY_PERMITTED = "input_text.whatsapp_bot_permitidos"

_pending_unlock: dict[str, "PendingUnlock"] = {}


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
        return f"Nao foi possivel destrancar: {_ha_error_detail(e)}"


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
    cache_name: str | None = None,
) -> GeminiAssistant:
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    if cache_name is None and settings.gemini_api_key and catalog.devices:
        cache_name = ensure_catalog_cache(
            api_key=settings.gemini_api_key,
            model=model_name,
            catalog=catalog,
            ttl_hours=settings.gemini_cache_ttl_hours,
        )
    return GeminiAssistant(
        settings.gemini_api_key,
        model=model_name,
        cache_name=cache_name,
        catalog_fallback=catalog.build_catalog_context(),
    )


async def execute_decision(
    decision: dict[str, Any],
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    phone: str,
    user_text: str,
    entities_context_used: bool,
) -> str:
    action = str(decision.get("action") or "reply").lower()
    reply = str(decision.get("response") or "").strip()
    log.info("execute_decision phone=%s action=%s", phone, action)

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

        domain = domain.strip()
        service = service.strip()
        raw_svc_data = dict(svc_data) if isinstance(svc_data, dict) else {}

        target = extract_target_entity_id(domain, service, raw_svc_data, decision.get("entity_id"))
        if not target:
            return await finalize("Informe entity_id no comando.")

        svc_data = build_ha_service_data(domain, service, target, raw_svc_data)
        if raw_svc_data != svc_data:
            log.info(
                "service_data normalizado entity=%s raw=%s -> ha=%s",
                target,
                raw_svc_data,
                svc_data,
            )

        allowed = catalog.actionable_entity_ids()
        if not allowed:
            return "Nenhum dispositivo configurado para acoes. Edite /config/shakira_devices.yaml."
        if target not in allowed:
            return (
                f"Nao posso alterar `{target}`. "
                "So posso agir nas entidades marcadas como acionaveis no catalogo."
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
                if reply:
                    return f"{reply}\n\n{prompt}"
                return prompt

        log.info("Chamando HA phone=%s %s/%s entity=%s", phone, domain, service, target)
        _log_service_payload("call_service", svc_data)
        try:
            result = await ha.call_service(domain, service, svc_data or None)
            _pending_unlock.pop(phone, None)
            log.info("HA call_service OK phone=%s %s/%s entity=%s", phone, domain, service, target)
            extra = ""
            if isinstance(result, dict) and result.get("service_response"):
                extra = str(result["service_response"])[:1500]
            elif result not in (None, "", []):
                extra = str(result)[:1500]
            return await finalize(extra if extra else "")
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
            return await finalize(f"Erro ao executar: {_ha_error_detail(e)}")

    if action == "search_photos":
        return await finalize(
            "Pedido de fotos recebido. Se nada chegar, verifique PhotoPrism nas opcoes do add-on."
        )

    return reply or "Nao entendi o proximo passo."


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
        "album",
        "query",
        "after",
        "before",
        "taken",
    ):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
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


async def handle_search_photos(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
) -> None:
    """Busca fotos no PhotoPrism e envia pelo WhatsApp."""
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        log.error("Envio de fotos bloqueado: Evolution nao configurado")
        return

    intro = str(decision.get("response") or "").strip()
    if not settings.photoprism_url or not settings.photoprism_token:
        msg = (
            intro + "\n\nPhotoPrism nao configurado. "
            "Defina photoprism_url e photoprism_token nas opcoes do add-on."
        ).strip()
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=_truncate_whatsapp(msg),
        )
        return

    filters = _parse_photo_filters(decision)
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

    try:
        photos, preview_token = await pp.search_photos(filters=filters, count=count)
    except PhotoprismAuthError:
        msg = (intro + "\n\nErro de autenticacao no PhotoPrism. Verifique o token.").strip()
        await evo.send_text(
            base_url=evo_base, api_key=evo_key, instance=instance, number=phone, text=msg
        )
        return
    except PhotoprismError as e:
        log.error("PhotoPrism falhou: %s", e, exc_info=True)
        extra = ""
        diag = getattr(e, "diagnostic", None) or {}
        if isinstance(diag, dict) and diag.get("hints"):
            extra = f"\n\nDica: {diag['hints'][0]}"
        msg = (intro + f"\n\nNao consegui buscar fotos: {e}{extra}").strip()
        await evo.send_text(
            base_url=evo_base, api_key=evo_key, instance=instance, number=phone, text=_truncate_whatsapp(msg)
        )
        return

    if not photos:
        msg = (intro + "\n\nNao encontrei fotos com esses criterios.").strip() or (
            "Nao encontrei fotos com esses criterios."
        )
        await evo.send_text(
            base_url=evo_base, api_key=evo_key, instance=instance, number=phone, text=msg
        )
        return

    if intro:
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=_truncate_whatsapp(intro),
        )

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
    permitted_raw = await fetch_permitted_phones_raw(ha)
    permitted = parse_allowed_numbers(permitted_raw)
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    default_inst = settings.evolution_instance.strip()

    if not permitted:
        log.warning("Nenhum numero permitido em %s", ENTITY_PERMITTED)
    if not evo_base or not evo_key:
        log.warning("Evolution URL ou api key ausentes nas opcoes do add-on")

    assistant = build_gemini_assistant(settings, catalog, cache_name=gemini_cache_name)

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

        pending_reply = await try_handle_pending_password(
            phone_norm,
            user_text or "",
            ha=ha,
            catalog=catalog,
        )
        if pending_reply is not None:
            log.info("Resposta senha pendente phone=%s: %s", phone_norm, pending_reply[:120])
            reply_text = _truncate_whatsapp(pending_reply)
            hint = _inst_hint if isinstance(_inst_hint, str) and _inst_hint.strip() else ""
            send_instance = (hint or str(webhook_instance) or default_inst or settings.evolution_instance).strip()
            if evo_base and evo_key and send_instance:
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=send_instance,
                    number=phone_norm,
                    text=reply_text,
                )
                record_exchange(phone_norm, user_text or "", reply_text)
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

        history_entries = get_recent(phone_norm)
        history_text = format_for_prompt(history_entries)
        log.info(
            "Historico phone=%s mensagens=%s chars=%s",
            phone_norm,
            len(history_entries),
            len(history_text),
        )

        decision = await asyncio.to_thread(
            assistant.decide,
            user_message=user_text or "",
            entities_context=ctx,
            conversation_history=history_text,
        )
        _log_gemini_decision(phone_norm, decision)

        hint = _inst_hint if isinstance(_inst_hint, str) and _inst_hint.strip() else ""
        action = str(decision.get("action") or "reply").lower()
        if action == "search_photos" and http is not None:
            send_instance = (
                hint or str(webhook_instance) or default_inst or settings.evolution_instance
            ).strip()
            if send_instance:
                await handle_search_photos(
                    decision,
                    settings=settings,
                    evo=evo,
                    http=http,
                    phone=phone_norm,
                    instance=send_instance,
                )
                photo_reply = str(decision.get("response") or "").strip() or "Fotos enviadas."
                record_exchange(phone_norm, user_text or "", photo_reply)
            continue

        reply_text = await execute_decision(
            decision,
            ha=ha,
            catalog=catalog,
            phone=phone_norm,
            user_text=user_text or "",
            entities_context_used=True,
        )

        reply_text = _truncate_whatsapp(reply_text)

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
        record_exchange(phone_norm, user_text or "", reply_text)
