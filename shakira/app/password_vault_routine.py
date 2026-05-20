"""Rotina dedicada para cofre de senhas (guardar/recuperar com encriptacao)."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.conversation_history import record_exchange
from app.evolution import EvolutionClient, parse_message_key, remote_jid_for_number
from app.password_vault_store import PasswordVaultStore, VaultPending, get_vault_store
from app.password_security import try_delete_inbound_password_message
from app.user_friendly import polish_user_message
from app.vault_crypto import vault_configured, vault_master_key
from app.whatsapp_steps import StepMessenger, TypingSession, pulse_whatsapp_typing, truncate_whatsapp

log = logging.getLogger(__name__)

SECRET_REVEAL_DELETE_SEC = 10.0

_CANCEL_RE = re.compile(r"^\s*(cancelar|cancela|nao|não|desistir)\s*[.!?]?\s*$", re.I)

_SAVE_INTENT_PATTERNS = (
    re.compile(r"\b(guardar|salvar|anotar|registrar|gravar)\b.*\bsenha\b", re.I),
    re.compile(r"\bsenha\b.*\b(guardar|salvar|anotar|registrar|gravar)\b", re.I),
    re.compile(r"\bcofre\b.*\bsenha\b", re.I),
    re.compile(r"\bsenha\b.*\bcofre\b", re.I),
)

_RETRIEVE_INTENT_PATTERNS = (
    re.compile(r"\b(qual|mostrar|mostra|buscar|recuperar|pegar|passa|me\s+d[aá])\b.*\bsenha\b", re.I),
    re.compile(r"\bsenha\b.*\b(de|do|da)\b", re.I),
    re.compile(r"^\s*senha\s+(de|do|da)\s+", re.I),
)

_LIST_INTENT_RE = re.compile(
    r"\b(listar|quais|minhas)\b.*\bsenhas?\b|\bsenhas?\s+(salvas|guardadas)\b",
    re.I,
)

_HA_PASSWORD_CONTEXT_RE = re.compile(
    r"\b(port[aã]o|porta\s+social|destranc|trancar|alarme|parti[cç][aã]o|cadeado)\b",
    re.I,
)

_LABEL_EXTRACT_RE = re.compile(
    r"senha(?:\s+(?:de|do|da))?\s+(.+?)\s*$",
    re.I,
)

_NOT_CONFIGURED_MSG = (
    "O cofre de senhas ainda nao esta configurado neste servidor. "
    "Defina a chave mestra nas opcoes do add-on (vault_master_key)."
)

_SAVE_LABEL_PROMPT = (
    "De qual servico, site ou conta e a senha?\n\n"
    "Envie *cancelar* para desistir."
)

_SAVE_SECRET_PROMPT = (
    "Agora envie a senha.\n\n"
    "Ela sera apagada desta conversa assim que for guardada."
)


@dataclass
class _SentSecretCleanup:
    phone: str
    message_key: dict[str, Any]
    evo: EvolutionClient
    evo_base: str
    evo_key: str
    instance: str


_pending_cleanups: list[asyncio.Task[None]] = []


def detect_vault_save_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or _HA_PASSWORD_CONTEXT_RE.search(text):
        return False
    return any(p.search(text) for p in _SAVE_INTENT_PATTERNS)


def detect_vault_retrieve_intent(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or _HA_PASSWORD_CONTEXT_RE.search(text):
        return False
    if detect_vault_save_intent(text):
        return False
    if _LIST_INTENT_RE.search(text):
        return True
    return any(p.search(text) for p in _RETRIEVE_INTENT_PATTERNS)


def extract_vault_label_query(user_text: str) -> str:
    text = (user_text or "").strip()
    m = _LABEL_EXTRACT_RE.search(text)
    if m:
        return m.group(1).strip(" .!?")
    for prefix in ("mostrar senha ", "qual senha ", "buscar senha ", "recuperar senha "):
        if text.casefold().startswith(prefix):
            return text[len(prefix) :].strip(" .!?")
    return ""


async def _send_text_and_key(
    *,
    phone: str,
    text: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> dict[str, Any] | None:
    body = truncate_whatsapp(polish_user_message(text))
    if messenger:
        await messenger.step(body)
        return None
    if not evo_base or not evo_key or not instance:
        return None
    await pulse_whatsapp_typing()
    return await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=body,
    )


async def _delete_message_key(
    *,
    phone: str,
    message_key: dict[str, Any],
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    msg_id = str(message_key.get("id") or "")
    if not msg_id:
        return False
    remote = str(message_key.get("remoteJid") or remote_jid_for_number(phone))
    return await evo.delete_message_for_everyone(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        message_id=msg_id,
        remote_jid=remote,
        from_me=bool(message_key.get("fromMe")),
        participant=str(message_key.get("participant") or "") or None,
    )


async def _finish_exchange(
    *,
    phone: str,
    user_text: str,
    messenger: StepMessenger | None,
    reply_text: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    redact_user: bool = False,
    redact_assistant: bool = False,
) -> None:
    if messenger and messenger.sent_any:
        assistant = messenger.combined()
        if redact_assistant:
            assistant = "Senha exibida (removida do chat por seguranca)."
        record_exchange(
            phone,
            "[senha omitida]" if redact_user else user_text,
            assistant,
        )
        return
    text = truncate_whatsapp(polish_user_message(reply_text))
    if not text:
        return
    if not evo_base or not evo_key or not instance:
        log.error("Cofre senhas: Evolution nao configurado phone=%s", phone)
        return
    await pulse_whatsapp_typing()
    await evo.send_text(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        text=text,
    )
    record_exchange(
        phone,
        "[senha omitida]" if redact_user else user_text,
        text,
    )


def _schedule_secret_cleanup(job: _SentSecretCleanup) -> None:
    async def _run() -> None:
        try:
            await asyncio.sleep(SECRET_REVEAL_DELETE_SEC)
            await _delete_message_key(
                phone=job.phone,
                message_key=job.message_key,
                evo=job.evo,
                evo_base=job.evo_base,
                evo_key=job.evo_key,
                instance=job.instance,
            )
            notice = "Apaguei a mensagem com a senha por seguranca."
            await _send_text_and_key(
                phone=job.phone,
                text=notice,
                evo=job.evo,
                evo_base=job.evo_base,
                evo_key=job.evo_key,
                instance=job.instance,
            )
            record_exchange(job.phone, "[auto]", notice)
        except Exception:
            log.exception("Falha ao apagar senha revelada phone=%s", job.phone)

    task = asyncio.create_task(_run())
    _pending_cleanups.append(task)
    _pending_cleanups[:] = [t for t in _pending_cleanups if not t.done()]


async def _reply_with_secret(
    *,
    phone: str,
    label: str,
    secret: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None,
) -> str:
    reply = f"Senha de *{label}*:\n{secret}"
    response = await _send_text_and_key(
        phone=phone,
        text=reply,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        messenger=messenger,
    )
    key = parse_message_key(response, phone=phone)
    if key:
        _schedule_secret_cleanup(
            _SentSecretCleanup(
                phone=phone,
                message_key=key,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
        )
    return reply


def _format_pick_prompt(labels: list[str]) -> str:
    lines = ["Encontrei varias senhas. Qual delas?", ""]
    for i, label in enumerate(labels[:10], start=1):
        lines.append(f"{i} — {label}")
    lines.append("\nResponda com o numero ou o nome. *cancelar* para desistir.")
    return "\n".join(lines)


def _resolve_pick(text: str, labels: list[str]) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(labels):
            return labels[idx - 1]
    low = raw.casefold()
    matches = [label for label in labels if label.casefold() == low]
    if len(matches) == 1:
        return matches[0]
    contains = [label for label in labels if low in label.casefold()]
    if len(contains) == 1:
        return contains[0]
    return None


async def _handle_save_pending(
    *,
    phone: str,
    text: str,
    pending: VaultPending,
    store: PasswordVaultStore,
    master_key: str,
    record: dict[str, Any] | None,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None,
) -> str:
    if pending.stage == "label":
        label = text.strip()
        if len(label) < 2:
            return "Preciso de um nome para identificar a senha (ex.: *wifi*, *email pessoal*)."
        pending.label = label[:120]
        pending.stage = "secret"
        store.save_pending(pending)
        return _SAVE_SECRET_PROMPT

    if pending.stage == "secret":
        secret = text.strip()
        if len(secret) < 1:
            return "A senha nao pode estar vazia. Envie novamente ou *cancelar*."
        try:
            store.upsert_entry(master_key=master_key, label=pending.label, secret=secret)
        except ValueError as e:
            store.clear_pending()
            return f"Nao foi possivel guardar: {e}"
        store.clear_pending()
        deleted = await try_delete_inbound_password_message(
            record=record,
            user_text=text,
            should_treat_as_password=True,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
        )
        confirm = f"Senha guardada para *{pending.label}*."
        if deleted:
            confirm += "\n\nApaguei a mensagem com a senha no WhatsApp por seguranca."
        return confirm

    store.clear_pending()
    return "Algo deu errado no cofre. Tente de novo."


async def _handle_retrieve_pick(
    *,
    phone: str,
    text: str,
    pending: VaultPending,
    store: PasswordVaultStore,
    master_key: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None,
) -> str:
    label = _resolve_pick(text, pending.candidates)
    if not label:
        return _format_pick_prompt(pending.candidates)
    matches = store.find_entries(label)
    if not matches:
        store.clear_pending()
        return f"Nao encontrei senha para *{label}*."
    store.clear_pending()
    secret = store.decrypt_entry(matches[0], master_key=master_key)
    return await _reply_with_secret(
        phone=phone,
        label=matches[0].label,
        secret=secret,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        messenger=messenger,
    )


async def _start_retrieve(
    *,
    phone: str,
    query: str,
    store: PasswordVaultStore,
    master_key: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None,
) -> str:
    labels = store.list_labels()
    if not labels:
        return "Ainda nao ha senhas guardadas no seu cofre."

    if _LIST_INTENT_RE.search(query) and not extract_vault_label_query(query):
        lines = ["Senhas guardadas:", ""] + [f"• {label}" for label in labels[:30]]
        if len(labels) > 30:
            lines.append(f"\n(+ {len(labels) - 30} outras)")
        lines.append("\nPara ver uma, diga por exemplo: *qual a senha do wifi*")
        return "\n".join(lines)

    label_query = extract_vault_label_query(query)
    matches = store.find_entries(label_query) if label_query else []

    if not label_query:
        if len(labels) == 1:
            matches = store.find_entries(labels[0])
        else:
            pending = VaultPending(
                mode="retrieve_pick",
                stage="pick",
                candidates=labels[:10],
            )
            store.save_pending(pending)
            return _format_pick_prompt(pending.candidates)

    if not matches:
        return f"Nao encontrei senha para *{label_query or query}*."

    if len(matches) > 1:
        pending = VaultPending(
            mode="retrieve_pick",
            stage="pick",
            candidates=[m.label for m in matches[:10]],
        )
        store.save_pending(pending)
        return _format_pick_prompt(pending.candidates)

    secret = store.decrypt_entry(matches[0], master_key=master_key)
    return await _reply_with_secret(
        phone=phone,
        label=matches[0].label,
        secret=secret,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        messenger=messenger,
    )


async def try_handle_password_vault_inbound(
    phone: str,
    user_text: str,
    *,
    record: dict[str, Any] | None,
    settings: Any,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    """
    Cofre de senhas dedicado (nao passa pelo Gemini).
    Retorna True se a mensagem foi consumida.
    """
    text = (user_text or "").strip()
    store = get_vault_store(phone)
    pending = store.load_pending()
    settings_key = getattr(settings, "vault_master_key", "")
    master_key = vault_master_key(settings_key)

    if not pending and not detect_vault_save_intent(text) and not detect_vault_retrieve_intent(text):
        return False

    if not evo_base or not evo_key or not instance:
        log.error("Cofre senhas sem Evolution phone=%s", phone)
        return True

    messenger = StepMessenger(
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        phone=phone,
    )
    redact_user = False
    redact_assistant = False
    reply = ""

    async with TypingSession(
        evo, evo_base=evo_base, evo_key=evo_key, instance=instance, phone=phone
    ):
        if pending and _CANCEL_RE.match(text):
            store.clear_pending()
            reply = "Ok, cancelado."
        elif not vault_configured(settings_key):
            store.clear_pending()
            reply = _NOT_CONFIGURED_MSG
        elif pending and pending.mode == "save":
            was_secret = pending.stage == "secret"
            reply = await _handle_save_pending(
                phone=phone,
                text=text,
                pending=pending,
                store=store,
                master_key=master_key,
                record=record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
                messenger=messenger,
            )
            redact_user = was_secret
        elif pending and pending.mode == "retrieve_pick":
            reply = await _handle_retrieve_pick(
                phone=phone,
                text=text,
                pending=pending,
                store=store,
                master_key=master_key,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
                messenger=messenger,
            )
            redact_assistant = messenger.sent_any
        elif detect_vault_save_intent(text):
            store.save_pending(VaultPending(mode="save", stage="label"))
            reply = _SAVE_LABEL_PROMPT
        else:
            reply = await _start_retrieve(
                phone=phone,
                query=text,
                store=store,
                master_key=master_key,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
                messenger=messenger,
            )
            redact_assistant = messenger.sent_any

        if reply and not messenger.sent_any:
            await messenger.step(reply, final=True)

    await _finish_exchange(
        phone=phone,
        user_text=text,
        messenger=messenger,
        reply_text=reply,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        redact_user=redact_user,
        redact_assistant=redact_assistant,
    )
    return True
