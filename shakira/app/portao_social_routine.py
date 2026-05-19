"""Rotina codificada para abrir o portao social (acesso rapido, sem Gemini)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.devices_catalog import DevicesCatalog
from app.homeassistant import HomeAssistantClient
from app.user_friendly import polish_user_message
from app.whatsapp_steps import StepMessenger, TypingSession

log = logging.getLogger(__name__)

PORTAO_SOCIAL_PASSWORD = "7896"

ENTITY_ALARM = "alarm_control_panel.amt_8000_partition_1"
ENTITY_ALIMENTACAO = "switch.alimentacao_fechadura_principal"
ENTITY_CADEADO = "lock.cadeado_portao_social_cadeado_portao_social"
ENTITY_FECHADURA = "switch.fechadura_portao_social"
ENTITY_SENSOR = "sensor.amt_8000_zone_1"
ENTITY_SCENE_SERVICO = "scene.abrir_portao_de_servico"

ROUTINE_ENTITIES = (
    ENTITY_ALARM,
    ENTITY_ALIMENTACAO,
    ENTITY_CADEADO,
    ENTITY_FECHADURA,
    ENTITY_SENSOR,
    ENTITY_SCENE_SERVICO,
)

_PASSWORD_PROMPT = (
    "Para abrir o portao social preciso confirmar o codigo de acesso. "
    "Qual o codigo? (Envie *cancelar* para desistir.)"
)

_FALLBACK_PROMPT = (
    "Nao consegui abrir o portao social completamente. O que deseja fazer?\n\n"
    "1 — Acionar a fechadura do portao social mesmo assim\n"
    "2 — Abrir o portao de servico\n"
    "3 — Cancelar"
)

_CANCEL_RE = re.compile(r"^\s*(cancelar|cancela|nao|não|desistir)\s*[.!?]?\s*$", re.I)
_SERVICO_ONLY_RE = re.compile(r"port[aã]o\s+de\s+servi", re.I)
_DIGITS_RE = re.compile(r"\b(\d{4,8})\b")

_INTENT_PATTERNS = (
    re.compile(r"entrar\s+(em\s+)?casa", re.I),
    re.compile(r"abrir\s+(o\s+)?port[aã]o(\s+social)?", re.I),
    re.compile(r"abre\s+(o\s+)?port[aã]o(\s+social)?", re.I),
    re.compile(r"quero\s+entrar", re.I),
    re.compile(r"deixa(r)?(-me)?\s+entrar", re.I),
    re.compile(r"preciso\s+entrar(\s+em\s+casa)?", re.I),
    re.compile(r"abrir\s+(a\s+)?(porta|entrada)\s+(social|principal)", re.I),
)

_FALLBACK_CHOICE_RE = {
    "fechadura": re.compile(r"^\s*(1|fechadura|mesmo\s+assim|for[cç]ar)\b", re.I),
    "servico": re.compile(
        r"^\s*(2|servi[cç]o|port[aã]o\s+de\s+servi[cç]o|portao\s+servico)\b", re.I
    ),
    "cancelar": re.compile(r"^\s*(3|cancelar|cancela|nao|não|desistir)\b", re.I),
}

_SENSOR_OPEN = frozenset({"open", "on"})
_ARMED_PREFIX = "armed"


@dataclass
class _PendingPortaoSocial:
    stage: str  # password | fallback
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.monotonic()


_pending: dict[str, _PendingPortaoSocial] = {}


def detect_portao_social_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    low = text.lower()
    if _SERVICO_ONLY_RE.search(low) and "social" not in low:
        return False
    return any(p.search(text) for p in _INTENT_PATTERNS)


def _extract_password_from_text(user_text: str) -> str | None:
    m = _DIGITS_RE.search(user_text or "")
    return m.group(1) if m else None


def _state_str(st: dict[str, Any] | None) -> str:
    if not st:
        return ""
    return str(st.get("state") or "").strip().lower()


def _is_sensor_open(state: str) -> bool:
    return state in _SENSOR_OPEN


def _needs_disarm(alarm_state: str) -> bool:
    if not alarm_state or alarm_state in ("unavailable", "unknown"):
        return False
    if alarm_state == "disarmed":
        return False
    return alarm_state.startswith(_ARMED_PREFIX) or alarm_state in (
        "pending",
        "arming",
    )


def _is_locked(lock_state: str) -> bool:
    return lock_state in ("locked", "locking")


def _is_off(switch_state: str) -> bool:
    return switch_state in ("off", "unknown", "unavailable", "")


async def _fetch_states(
    ha: HomeAssistantClient, entity_ids: tuple[str, ...]
) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    for eid in entity_ids:
        out[eid] = await ha.get_state(eid)
    return out


async def _call_service_safe(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    domain: str,
    service: str,
    entity_id: str,
) -> tuple[bool, str]:
    svc_data = catalog.apply_service_defaults(entity_id, {"entity_id": entity_id})
    try:
        await ha.call_service(domain, service, svc_data)
        return True, ""
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = (exc.response.text or "")[:200]
        except Exception:
            detail = str(exc.response.status_code)
        log.warning(
            "Portao social: falha %s/%s entity=%s status=%s %s",
            domain,
            service,
            entity_id,
            exc.response.status_code,
            detail,
        )
        return False, detail or str(exc.response.status_code)


async def _run_open_routine(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> bool:
    """Executa sequencia completa. Retorna True se sensor indicar portao aberto."""
    await messenger.step("A iniciar abertura do portao social...")

    states = await _fetch_states(ha, ROUTINE_ENTITIES)
    sensor_state = _state_str(states.get(ENTITY_SENSOR))
    if _is_sensor_open(sensor_state):
        await messenger.step("O portao social ja esta aberto.", final=True)
        return True

    failed_steps: list[str] = []

    alarm_state = _state_str(states.get(ENTITY_ALARM))
    if _needs_disarm(alarm_state):
        await messenger.step("Desarmando a particao 1 do alarme (perimetro externo)...")
        ok, err = await _call_service_safe(
            ha, catalog, "alarm_control_panel", "alarm_disarm", ENTITY_ALARM
        )
        if ok:
            await messenger.step("Alarme desarmado.")
        else:
            failed_steps.append("desarmar alarme")
            await messenger.step(f"Nao consegui desarmar o alarme ({err or 'erro'}).")

    alim_state = _state_str(states.get(ENTITY_ALIMENTACAO))
    if _is_off(alim_state):
        await messenger.step("Ligando alimentacao da fechadura...")
        ok, err = await _call_service_safe(
            ha, catalog, "switch", "turn_on", ENTITY_ALIMENTACAO
        )
        if ok:
            await messenger.step("Alimentacao ligada.")
            await asyncio.sleep(0.5)
        else:
            failed_steps.append("ligar alimentacao")
            await messenger.step(f"Nao consegui ligar a alimentacao ({err or 'erro'}).")

    cadeado_state = _state_str(states.get(ENTITY_CADEADO))
    if _is_locked(cadeado_state):
        await messenger.step("Destrancando cadeado do portao...")
        ok, err = await _call_service_safe(ha, catalog, "lock", "unlock", ENTITY_CADEADO)
        if ok:
            await messenger.step("Cadeado destrancado.")
            await asyncio.sleep(0.5)
        else:
            failed_steps.append("destrancar cadeado")
            await messenger.step(f"Nao consegui destrancar o cadeado ({err or 'erro'}).")

    await messenger.step("Acionando fechadura do portao social...")
    ok, err = await _call_service_safe(
        ha, catalog, "switch", "turn_on", ENTITY_FECHADURA
    )
    if not ok:
        failed_steps.append("acionar fechadura")
        await messenger.step(f"Nao consegui acionar a fechadura ({err or 'erro'}).")
    else:
        await asyncio.sleep(2.5)
        st_after = await ha.get_state(ENTITY_SENSOR)
        if _is_sensor_open(_state_str(st_after)):
            await messenger.step("Portao social aberto com sucesso!", final=True)
            return True
        await messenger.step("Fechadura acionada, mas o sensor ainda indica portao fechado.")

    if failed_steps:
        log.info("Portao social: rotina incompleta falhas=%s", failed_steps)
    return False


async def _run_fallback_fechadura(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> None:
    await messenger.step("A acionar apenas a fechadura do portao social...")
    ok, err = await _call_service_safe(
        ha, catalog, "switch", "turn_on", ENTITY_FECHADURA
    )
    if ok:
        await asyncio.sleep(2.0)
        st = await ha.get_state(ENTITY_SENSOR)
        if _is_sensor_open(_state_str(st)):
            await messenger.step("Portao aberto.", final=True)
        else:
            await messenger.step(
                "Fechadura acionada. Verifique se o portao abriu.", final=True
            )
    else:
        await messenger.step(
            f"Nao foi possivel acionar a fechadura ({err or 'erro'}).", final=True
        )


async def _run_fallback_servico(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> None:
    await messenger.step("A abrir o portao de servico...")
    ok, err = await _call_service_safe(
        ha, catalog, "scene", "turn_on", ENTITY_SCENE_SERVICO
    )
    if ok:
        await messenger.step("Portao de servico acionado.", final=True)
    else:
        await messenger.step(
            f"Nao foi possivel abrir o portao de servico ({err or 'erro'}).", final=True
        )


def _parse_fallback_choice(user_text: str) -> str | None:
    text = (user_text or "").strip()
    if not text:
        return None
    for choice, pattern in _FALLBACK_CHOICE_RE.items():
        if pattern.search(text):
            return choice
    return None


def clear_pending(phone: str) -> None:
    _pending.pop(phone, None)


async def _finish_exchange(
    *,
    phone: str,
    user_text: str,
    messenger: StepMessenger | None,
    reply_text: str,
    evo: Any,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> None:
    from app.conversation_history import record_exchange
    from app.user_friendly import polish_user_message
    from app.whatsapp_steps import pulse_whatsapp_typing, truncate_whatsapp

    if messenger and messenger.sent_any:
        record_exchange(phone, user_text, messenger.combined())
        return
    text = truncate_whatsapp(polish_user_message(reply_text))
    if not text:
        return
    if not evo_base or not evo_key or not instance:
        log.error("Portao social: Evolution nao configurado no fechamento phone=%s", phone)
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


async def try_handle_portao_social_inbound(
    phone: str,
    user_text: str,
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    evo: Any,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    """
    Trata pedidos de abrir portao social / entrar em casa.
    Retorna True se a mensagem foi consumida (nao chamar Gemini).
    """
    text = (user_text or "").strip()
    pending = _pending.get(phone)

    if pending and pending.stage == "fallback":
        choice = _parse_fallback_choice(text)
        if choice is None:
            reply = (
                "Nao entendi a opcao. Responda com:\n"
                "1 — fechadura\n2 — portao de servico\n3 — cancelar"
            )
            if evo_base and evo_key and instance:
                messenger = StepMessenger(
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                    phone=phone,
                )
                await _finish_exchange(
                    phone=phone,
                    user_text=text,
                    messenger=messenger,
                    reply_text=reply,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
            return True

        clear_pending(phone)
        if choice == "cancelar":
            reply = "Ok, cancelado."
            if evo_base and evo_key and instance:
                messenger = StepMessenger(
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                    phone=phone,
                )
                await _finish_exchange(
                    phone=phone,
                    user_text=text,
                    messenger=messenger,
                    reply_text=reply,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
            return True

        if not evo_base or not evo_key or not instance:
            log.error("Portao social fallback sem Evolution configurado phone=%s", phone)
            return True

        messenger = StepMessenger(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
        )
        async with TypingSession(
            evo, evo_base=evo_base, evo_key=evo_key, instance=instance, phone=phone
        ):
            if choice == "fechadura":
                await _run_fallback_fechadura(ha=ha, catalog=catalog, messenger=messenger)
            else:
                await _run_fallback_servico(ha=ha, catalog=catalog, messenger=messenger)

        await _finish_exchange(
            phone=phone,
            user_text=text,
            messenger=messenger,
            reply_text="",
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    if pending and pending.stage == "password":
        if _CANCEL_RE.match(text):
            clear_pending(phone)
            reply = "Abertura do portao social cancelada."
            if evo_base and evo_key and instance:
                messenger = StepMessenger(
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                    phone=phone,
                )
                await _finish_exchange(
                    phone=phone,
                    user_text=text,
                    messenger=messenger,
                    reply_text=reply,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
            return True

        candidate = text if re.fullmatch(r"\d{4,8}", text) else None
        if not candidate or candidate != PORTAO_SOCIAL_PASSWORD:
            reply = "Codigo incorreto. Tente novamente ou envie *cancelar*."
            if evo_base and evo_key and instance:
                messenger = StepMessenger(
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                    phone=phone,
                )
                await _finish_exchange(
                    phone=phone,
                    user_text=text,
                    messenger=messenger,
                    reply_text=reply,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
            return True

        clear_pending(phone)
        if not evo_base or not evo_key or not instance:
            log.error("Portao social rotina sem Evolution phone=%s", phone)
            return True

        messenger = StepMessenger(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
        )
        async with TypingSession(
            evo, evo_base=evo_base, evo_key=evo_key, instance=instance, phone=phone
        ):
            success = await _run_open_routine(ha=ha, catalog=catalog, messenger=messenger)
            if not success:
                _pending[phone] = _PendingPortaoSocial(stage="fallback")
                await messenger.step(_FALLBACK_PROMPT, final=True)

        await _finish_exchange(
            phone=phone,
            user_text=text,
            messenger=messenger,
            reply_text="",
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    if not detect_portao_social_intent(text):
        return False

    pwd = _extract_password_from_text(text)
    if pwd == PORTAO_SOCIAL_PASSWORD:
        if not evo_base or not evo_key or not instance:
            log.error("Portao social intent+senha sem Evolution phone=%s", phone)
            return True

        messenger = StepMessenger(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
        )
        async with TypingSession(
            evo, evo_base=evo_base, evo_key=evo_key, instance=instance, phone=phone
        ):
            success = await _run_open_routine(ha=ha, catalog=catalog, messenger=messenger)
            if not success:
                _pending[phone] = _PendingPortaoSocial(stage="fallback")
                await messenger.step(_FALLBACK_PROMPT, final=True)

        await _finish_exchange(
            phone=phone,
            user_text=text,
            messenger=messenger,
            reply_text="",
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        return True

    _pending[phone] = _PendingPortaoSocial(stage="password")
    reply = polish_user_message(_PASSWORD_PROMPT)
    if evo_base and evo_key and instance:
        messenger = StepMessenger(
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            phone=phone,
        )
        await _finish_exchange(
            phone=phone,
            user_text=text,
            messenger=messenger,
            reply_text=reply,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
    else:
        from app.conversation_history import record_exchange

        record_exchange(phone, text, reply)
    log.info("Portao social: pedido codigo phone=%s", phone)
    return True
