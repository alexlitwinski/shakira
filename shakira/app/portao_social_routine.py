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
from app.pending_flow_utils import should_abandon_pending_flow
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
ENTITY_SCENE_GRADE = "scene.abrir_grade_lateral"
ENTITY_PORTA_SOCIAL = "lock.porta_social"
ENTITY_PORTA_COZINHA = "lock.porta_cozinha"

ROUTINE_ENTITIES = (
    ENTITY_ALARM,
    ENTITY_ALIMENTACAO,
    ENTITY_CADEADO,
    ENTITY_FECHADURA,
    ENTITY_SENSOR,
    ENTITY_SCENE_SERVICO,
)

_SENSOR_CHECK_DELAY_SEC = 5.0

_PASSWORD_PROMPT = (
    "Para abrir o portão social preciso confirmar o código de acesso. "
    "Qual o código? (Envie *cancelar* para desistir.)"
)

_FALLBACK_PROMPT = (
    "Não consegui abrir o portão social completamente. O que deseja fazer?\n\n"
    "1 — Acionar a fechadura do portão social mesmo assim\n"
    "2 — Abrir o portão de serviço\n"
    "3 — Cancelar"
)

_FOLLOWUP_PROMPT = (
    "Portão social aberto! Quer que eu abra também?\n\n"
    "1 — Grade lateral\n"
    "2 — Porta social\n"
    "3 — Porta da cozinha\n"
    "4 — Todos\n"
    "5 — Não, obrigado"
)

_CANCEL_RE = re.compile(r"^\s*(cancelar|cancela|nao|não|desistir)\s*[.!?]?\s*$", re.I)
_PORTAO_SERVICO_RE = re.compile(
    r"\b(?:abrir?|abre|abra|acione|aciona|activ|ativa|destranc?a|destranque|liber[ae]|comandar?|destrancar?)\b.*\bport[aã]o\s+(?:de\s+)?servi",
    re.I,
)
_DIGITS_RE = re.compile(r"\b(\d{4,8})\b")

_INTENT_PATTERNS = (
    re.compile(r"entrar\s+(em\s+)?casa", re.I),
    re.compile(r"abr[ae]\s+(o\s+)?port[aã]o\s+social\b", re.I),
    re.compile(r"abr[ae]\s+(o\s+)?port[aã]o(?!\s+(?:de\s+)?servi)", re.I),
    re.compile(r"abrir\s+(o\s+)?port[aã]o\s+social\b", re.I),
    re.compile(r"abre\s+(o\s+)?port[aã]o\s+social\b", re.I),
    re.compile(r"abrir\s+(o\s+)?port[aã]o(?!\s+(?:de\s+)?servi)", re.I),
    re.compile(r"abre\s+(o\s+)?port[aã]o(?!\s+(?:de\s+)?servi)", re.I),
    re.compile(r"quero\s+entrar", re.I),
    re.compile(r"deixa(r)?(-me)?\s+entrar", re.I),
    re.compile(r"preciso\s+entrar(\s+em\s+casa)?", re.I),
    re.compile(r"abrir\s+(a\s+)?(porta|entrada)\s+(social|principal)", re.I),
    re.compile(r"abr[ae]\s+(a\s+)?(porta|entrada)\s+(social|principal)", re.I),
)

_FALLBACK_CHOICE_RE = {
    "fechadura": re.compile(r"^\s*(1|fechadura|mesmo\s+assim|for[cç]ar)\b", re.I),
    "servico": re.compile(
        r"^\s*(2|servi[cç]o|port[aã]o\s+de\s+servi[cç]o|portao\s+servico)\b", re.I
    ),
    "cancelar": re.compile(r"^\s*(3|cancelar|cancela|nao|não|desistir)\b", re.I),
}

_FOLLOWUP_CHOICE_RE = {
    "grade": re.compile(r"^\s*(1|grade(\s+lateral)?)\b", re.I),
    "porta_social": re.compile(r"^\s*(2|porta\s+social)\b", re.I),
    "cozinha": re.compile(r"^\s*(3|porta(\s+da)?\s+cozinha|cozinha)\b", re.I),
    "todos": re.compile(r"^\s*(4|todos|tudo)\b", re.I),
    "nao": re.compile(r"^\s*(5|nao|não|obrigado|nada|cancelar)\b", re.I),
}

_SENSOR_OPEN = frozenset({"open", "on"})
_ARMED_PREFIX = "armed"


@dataclass
class _PendingPortaoSocial:
    stage: str  # password | fallback | followup
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.monotonic()


_pending: dict[str, _PendingPortaoSocial] = {}


def detect_portao_servico_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    low = text.casefold()
    if "social" in low and "servi" not in low:
        return False
    return bool(_PORTAO_SERVICO_RE.search(text))


def detect_portao_social_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if detect_portao_servico_intent(text):
        return False
    low = text.lower()
    if "servi" in low and "port" in low:
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


async def _get_fresh_sensor_state(ha: HomeAssistantClient) -> str:
    """Consulta o sensor do portao direto na API HA (invalida cache local antes)."""
    from app.ha_states_cache import invalidate_ha_states_cache

    invalidate_ha_states_cache()
    st = await ha.get_state(ENTITY_SENSOR)
    return _state_str(st)


async def _is_portao_open(ha: HomeAssistantClient) -> bool:
    await asyncio.sleep(_SENSOR_CHECK_DELAY_SEC)
    return _is_sensor_open(await _get_fresh_sensor_state(ha))


async def _call_service_safe(
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    domain: str,
    service: str,
    entity_id: str,
    *,
    extra_data: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    svc_data = catalog.apply_service_defaults(entity_id, {"entity_id": entity_id})
    if extra_data:
        svc_data.update(extra_data)
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
    await messenger.step("A iniciar abertura do portão social...")

    states = await _fetch_states(ha, ROUTINE_ENTITIES)
    sensor_state = _state_str(states.get(ENTITY_SENSOR))
    if _is_sensor_open(sensor_state):
        await messenger.step("O portão social já está aberto.")
        return True

    failed_steps: list[str] = []

    alarm_state = _state_str(states.get(ENTITY_ALARM))
    if _needs_disarm(alarm_state):
        await messenger.step("Desarmando a partição 1 do alarme (perímetro externo)...")
        ok, err = await _call_service_safe(
            ha, catalog, "alarm_control_panel", "alarm_disarm", ENTITY_ALARM
        )
        if ok:
            await messenger.step("Alarme desarmado.")
        else:
            failed_steps.append("desarmar alarme")
            await messenger.step(f"Não consegui desarmar o alarme ({err or 'erro'}).")

    alim_state = _state_str(states.get(ENTITY_ALIMENTACAO))
    if _is_off(alim_state):
        await messenger.step("Ligando alimentação da fechadura...")
        ok, err = await _call_service_safe(
            ha, catalog, "switch", "turn_on", ENTITY_ALIMENTACAO
        )
        if ok:
            await messenger.step("Alimentação ligada.")
            await asyncio.sleep(0.5)
        else:
            failed_steps.append("ligar alimentacao")
            await messenger.step(f"Não consegui ligar a alimentação ({err or 'erro'}).")

    cadeado_state = _state_str(states.get(ENTITY_CADEADO))
    if _is_locked(cadeado_state):
        await messenger.step("Destrancando cadeado do portão...")
        ok, err = await _call_service_safe(ha, catalog, "lock", "unlock", ENTITY_CADEADO)
        if ok:
            await messenger.step("Cadeado destrancado.")
            await asyncio.sleep(0.5)
        else:
            failed_steps.append("destrancar cadeado")
            await messenger.step(f"Não consegui destrancar o cadeado ({err or 'erro'}).")

    await messenger.step("Acionando fechadura do portão social...")
    ok, err = await _call_service_safe(
        ha, catalog, "switch", "turn_on", ENTITY_FECHADURA
    )
    if not ok:
        failed_steps.append("acionar fechadura")
        await messenger.step(f"Não consegui acionar a fechadura ({err or 'erro'}).")
    else:
        if await _is_portao_open(ha):
            await messenger.step("Portão social aberto com sucesso!")
            return True
        await messenger.step("Fechadura acionada, mas o sensor ainda indica portão fechado.")

    if failed_steps:
        log.info("Portao social: rotina incompleta falhas=%s", failed_steps)
    return False


async def _run_fallback_fechadura(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> bool:
    await messenger.step("A acionar apenas a fechadura do portão social...")
    ok, err = await _call_service_safe(
        ha, catalog, "switch", "turn_on", ENTITY_FECHADURA
    )
    if ok:
        if await _is_portao_open(ha):
            await messenger.step("Portão aberto.")
            return True
        await messenger.step("Fechadura acionada. Verifique se o portão abriu.")
        return False
    await messenger.step(
        f"Não foi possível acionar a fechadura ({err or 'erro'})."
    )
    return False


async def _run_portao_servico(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> None:
    await messenger.step("A abrir o portão de serviço...")
    ok, err = await _call_service_safe(
        ha, catalog, "scene", "turn_on", ENTITY_SCENE_SERVICO
    )
    if ok:
        await messenger.step("Portão de serviço acionado.", final=True)
    else:
        await messenger.step(
            f"Não foi possível abrir o portão de serviço ({err or 'erro'}).", final=True
        )


async def _run_fallback_servico(
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> None:
    await _run_portao_servico(ha=ha, catalog=catalog, messenger=messenger)


def _parse_fallback_choice(user_text: str) -> str | None:
    text = (user_text or "").strip()
    if not text:
        return None
    for choice, pattern in _FALLBACK_CHOICE_RE.items():
        if pattern.search(text):
            return choice
    return None


def _parse_followup_choice(user_text: str) -> str | None:
    text = (user_text or "").strip()
    if not text:
        return None
    for choice, pattern in _FOLLOWUP_CHOICE_RE.items():
        if pattern.search(text):
            return choice
    return None


async def _run_followup_actions(
    choice: str,
    *,
    ha: HomeAssistantClient,
    catalog: DevicesCatalog,
    messenger: StepMessenger,
) -> None:
    steps: list[tuple[str, str, str, str, dict[str, Any] | None]] = []
    if choice in ("grade", "todos"):
        steps.append(("grade lateral", ENTITY_SCENE_GRADE, "scene", "turn_on", None))
    if choice in ("porta_social", "todos"):
        steps.append(
            (
                "porta social",
                ENTITY_PORTA_SOCIAL,
                "lock",
                "unlock",
                {"code": PORTAO_SOCIAL_PASSWORD},
            )
        )
    if choice in ("cozinha", "todos"):
        steps.append(("porta da cozinha", ENTITY_PORTA_COZINHA, "lock", "unlock", None))

    if not steps:
        return

    ok_labels: list[str] = []
    fail_labels: list[str] = []
    for label, entity_id, domain, service, extra in steps:
        await messenger.step(f"A abrir {label}...")
        ok, err = await _call_service_safe(
            ha, catalog, domain, service, entity_id, extra_data=extra
        )
        if ok:
            ok_labels.append(label)
        else:
            fail_labels.append(f"{label} ({err or 'erro'})")

    if ok_labels and not fail_labels:
        await messenger.step(f"Pronto: {', '.join(ok_labels)}.", final=True)
    elif ok_labels:
        await messenger.step(
            f"Concluido parcialmente — ok: {', '.join(ok_labels)}; "
            f"falhou: {', '.join(fail_labels)}.",
            final=True,
        )
    else:
        await messenger.step(
            f"Não consegui abrir: {', '.join(fail_labels)}.", final=True
        )


async def _offer_followup_or_fallback(
    *,
    phone: str,
    success: bool,
    messenger: StepMessenger,
) -> None:
    if success:
        _pending[phone] = _PendingPortaoSocial(stage="followup")
        await messenger.step(_FOLLOWUP_PROMPT, final=True)
    else:
        _pending[phone] = _PendingPortaoSocial(stage="fallback")
        await messenger.step(_FALLBACK_PROMPT, final=True)


def clear_pending(phone: str) -> None:
    _pending.pop(phone, None)


def has_portao_social_pending(phone: str) -> bool:
    return phone in _pending


def _drop_stale_pending(phone: str, text: str) -> bool:
    """Limpa pending expirado ou fora de contexto. True se foi abandonado."""
    pending = _pending.get(phone)
    if not pending:
        return False
    kind = "password" if pending.stage == "password" else "menu"
    if should_abandon_pending_flow(pending.created_at, text, pending_kind=kind):
        clear_pending(phone)
        log.info(
            "Portao social pending abandonado phone=%s stage=%s",
            phone,
            pending.stage,
        )
        return True
    return False


def is_password_stage_pending(phone: str) -> bool:
    pending = _pending.get(phone)
    return pending is not None and pending.stage == "password"


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


async def try_handle_portao_servico_inbound(
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
    """Abre o portao de servico (cena dedicada). Nao e a rotina do portao social."""
    text = (user_text or "").strip()
    if has_portao_social_pending(phone):
        if _drop_stale_pending(phone, text):
            pass
        else:
            return False
    if _pending.get(phone):
        return False
    if not detect_portao_servico_intent(text):
        return False

    if not evo_base or not evo_key or not instance:
        log.error("Portao servico sem Evolution phone=%s", phone)
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
        await _run_portao_servico(ha=ha, catalog=catalog, messenger=messenger)

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
    log.info("Portao servico tratado phone=%s", phone)
    return True


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
    message_record: dict[str, Any] | None = None,
) -> bool:
    """
    Trata pedidos de abrir portao social / entrar em casa.
    Retorna True se a mensagem foi consumida (nao chamar Gemini).
    """
    text = (user_text or "").strip()
    pending = _pending.get(phone)
    if pending:
        if pending.stage in ("fallback", "followup") and detect_portao_social_intent(text):
            clear_pending(phone)
            pending = None
        elif _drop_stale_pending(phone, text):
            return False
        else:
            pending = _pending.get(phone)

    if pending and pending.stage == "followup":
        choice = _parse_followup_choice(text)
        if choice is None:
            reply = (
                "Não entendi a opção. Responda com:\n"
                "1 — grade lateral\n2 — porta social\n3 — porta da cozinha\n"
                "4 — todos\n5 — não, obrigado"
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
        if choice == "nao":
            reply = "Ok, fico por aqui."
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
            log.error("Portao social followup sem Evolution configurado phone=%s", phone)
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
            await _run_followup_actions(
                choice, ha=ha, catalog=catalog, messenger=messenger
            )

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

    if pending and pending.stage == "fallback":
        choice = _parse_fallback_choice(text)
        if choice is None:
            reply = (
                "Não entendi a opção. Responda com:\n"
                "1 — fechadura\n2 — portão de serviço\n3 — cancelar"
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
                opened = await _run_fallback_fechadura(
                    ha=ha, catalog=catalog, messenger=messenger
                )
                if opened:
                    _pending[phone] = _PendingPortaoSocial(stage="followup")
                    await messenger.step(_FOLLOWUP_PROMPT, final=True)
                else:
                    await messenger.step(
                        "Não confirmei a abertura do portão.", final=True
                    )
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
            reply = "Abertura do portão social cancelada."
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
            reply = "Código incorreto. Tente novamente ou envie *cancelar*."
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
            await _offer_followup_or_fallback(
                phone=phone, success=success, messenger=messenger
            )

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
            await _offer_followup_or_fallback(
                phone=phone, success=success, messenger=messenger
            )

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
