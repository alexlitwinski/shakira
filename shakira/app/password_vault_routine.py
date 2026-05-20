"""Cofre de senhas: intencao via Gemini; execucao e passos pendentes aqui."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
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

_VAULT_LIST_RE = re.compile(
    r"(?:"
    r"\b(?:quais|listar?|mostrar?|mostre|ver)\b.*\b(?:senhas?|credenciais?)\b.*(?:"
    r"gravad\w*|guardad\w*|cofre"
    r")"
    r"|"
    r"\b(?:senhas?|credenciais?)\b.*(?:gravad\w*|guardad\w*|cofre)"
    r"|"
    r"\b(?:o\s+que\s+tenho|itens)\b.*\b(?:no\s+)?cofre\b"
    r"|"
    r"\bcofre\s+de\s+senhas?\b"
    r")",
    re.I,
)

_VAULT_RETRIEVE_RE = re.compile(
    r"(?:"
    r"(?:mostr(?:e|a)|qual(?:\s+é|\s+e)?|recuper(?:e|a)|busc(?:e|a)|"
    r"me\s+(?:d[ae]|manda|envia)|envi(?:e|a)|preciso\s+da?)\s+"
    r"(?:a\s+)?senha\s+(?:d[aoe]\s+|de\s+)?(?P<label_retrieve>.+?)\s*[.!?]?\s*$"
    r"|"
    r"senha\s+(?:d[aoe]\s+|de\s+)(?P<label_alt>.+?)\s*[.!?]?\s*$"
    r")",
    re.I,
)

_VAULT_SAVE_RE = re.compile(
    r"\b(?:guardar?|grava(?:r)?|salva(?:r)?|anotar?)\b.*\b(?:senha|credencial)\b",
    re.I,
)

_HA_LOCK_PASSWORD_RE = re.compile(
    r"\b(?:destranc|tranc|abre|fecha|porta|portão|portao|fechadura|lock)\b",
    re.I,
)

_NOT_CONFIGURED_MSG = (
    "O cofre de senhas ainda não está configurado neste servidor. "
    "Defina a chave mestra nas opções do add-on (vault_master_key)."
)

_SAVE_LABEL_PROMPT = (
    "De qual serviço, site ou conta é a senha?\n\n"
    "Envie *cancelar* para desistir."
)

_SAVE_SECRET_PROMPT = (
    "Agora envie a senha.\n\n"
    "Ela será apagada desta conversa assim que for guardada."
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


def _vault_fields(decision: dict[str, Any]) -> tuple[str, str]:
    label = str(decision.get("vault_label") or "").strip()
    secret = str(decision.get("vault_secret") or "").strip()
    return label[:120], secret


def classify_vault_intent(user_text: str) -> tuple[str, str]:
    """
    Detecta pedido direto ao cofre (sem Gemini).
    Retorna (acao, rotulo): acao em vault_list|vault_retrieve|vault_save|''.
    """
    text = (user_text or "").strip()
    if not text or _CANCEL_RE.match(text):
        return "", ""
    if _HA_LOCK_PASSWORD_RE.search(text) and not re.search(
        r"\b(?:cofre|wifi|conta|site|servi[cç]o|email|e-mail)\b", text, re.I
    ):
        return "", ""

    if _VAULT_LIST_RE.search(text):
        return "vault_list", ""

    m = _VAULT_SAVE_RE.search(text)
    if m:
        return "vault_save", ""

    m = _VAULT_RETRIEVE_RE.search(text)
    if m:
        label = (m.group("label_retrieve") or m.group("label_alt") or "").strip()
        label = re.sub(r"^(?:da|do|de|a|o)\s+", "", label, flags=re.I).strip(" .!?")
        if label and len(label) >= 2:
            return "vault_retrieve", label[:120]
        return "vault_retrieve", ""

    if re.search(
        r"\b(?:qual|mostre|mostra)\b.*\bsenha\b",
        text,
        re.I,
    ) and re.search(r"\b(?:cofre|gravad|guardad)\b", text, re.I):
        return "vault_retrieve", ""

    return "", ""


async def _send_text_and_key(
    *,
    phone: str,
    text: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None = None,
    schedule_secret_cleanup: bool = False,
) -> dict[str, Any] | None:
    body = truncate_whatsapp(polish_user_message(text))
    if messenger:
        await messenger.step(body)
        if schedule_secret_cleanup:
            key = messenger.last_message_key()
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
            else:
                log.warning(
                    "Cofre: sem message key para apagar senha revelada phone=%s",
                    phone,
                )
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
            assistant = "Senha exibida (removida do chat por segurança)."
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
            notice = "Apaguei a mensagem com a senha por segurança."
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
        schedule_secret_cleanup=True,
    )
    if not messenger:
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
        else:
            log.warning(
                "Cofre: sem message key para apagar senha revelada phone=%s",
                phone,
            )
    return reply


def _format_pick_prompt(labels: list[str]) -> str:
    lines = ["Encontrei várias senhas. Qual delas?", ""]
    for i, label in enumerate(labels[:10], start=1):
        lines.append(f"{i} — {label}")
    lines.append("\nResponda com o número ou o nome. *cancelar* para desistir.")
    return "\n".join(lines)


def _format_list_labels(labels: list[str]) -> str:
    if not labels:
        return "Ainda não há senhas guardadas no seu cofre."
    lines = ["Senhas guardadas:", ""] + [f"• {label}" for label in labels[:30]]
    if len(labels) > 30:
        lines.append(f"\n(+ {len(labels) - 30} outras)")
    lines.append("\nPara ver uma, peça a senha pelo nome (ex.: wifi, casa).")
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


async def _save_vault_entry(
    *,
    store: PasswordVaultStore,
    master_key: str,
    label: str,
    secret: str,
    user_text: str,
    record: dict[str, Any] | None,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> str:
    store.upsert_entry(master_key=master_key, label=label, secret=secret)
    deleted = await try_delete_inbound_password_message(
        record=record,
        user_text=user_text,
        should_treat_as_password=True,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
    )
    confirm = f"Senha guardada para *{label}*."
    if deleted:
        confirm += "\n\nApaguei a mensagem com a senha no WhatsApp por segurança."
    return confirm


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
        if pending.secret_prefill:
            try:
                return await _save_vault_entry(
                    store=store,
                    master_key=master_key,
                    label=pending.label,
                    secret=pending.secret_prefill,
                    user_text=text,
                    record=record,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
            except ValueError as e:
                store.clear_pending()
                return f"Não foi possível guardar: {e}"
            finally:
                store.clear_pending()
        pending.stage = "secret"
        store.save_pending(pending)
        return _SAVE_SECRET_PROMPT

    if pending.stage == "secret":
        secret = text.strip()
        if len(secret) < 1:
            return "A senha não pode estar vazia. Envie novamente ou *cancelar*."
        try:
            return await _save_vault_entry(
                store=store,
                master_key=master_key,
                label=pending.label,
                secret=secret,
                user_text=text,
                record=record,
                evo=evo,
                evo_base=evo_base,
                evo_key=evo_key,
                instance=instance,
            )
        except ValueError as e:
            store.clear_pending()
            return f"Não foi possível guardar: {e}"
        finally:
            store.clear_pending()

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
        return f"Não encontrei senha para *{label}*."
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


async def _handle_vault_retrieve(
    *,
    phone: str,
    label_query: str,
    store: PasswordVaultStore,
    master_key: str,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None,
) -> tuple[str, bool]:
    """Retorna (mensagem, redact_assistant se a senha foi enviada no chat)."""
    labels = store.list_labels()
    if not labels:
        return "Ainda não há senhas guardadas no seu cofre.", False

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
            return _format_pick_prompt(pending.candidates), False

    else:
        matches = store.find_entries(label_query)

    if not matches:
        hint = (
            f"Não encontrei senha para *{label_query}* no cofre encriptado.\n\n"
            "Se ainda não guardou aqui, diga por exemplo: "
            f"*guardar senha de {label_query}* e envie a senha quando eu pedir."
        )
        return hint, False

    if len(matches) > 1:
        pending = VaultPending(
            mode="retrieve_pick",
            stage="pick",
            candidates=[m.label for m in matches[:10]],
        )
        store.save_pending(pending)
        return _format_pick_prompt(pending.candidates), False

    await _reply_with_secret(
        phone=phone,
        label=matches[0].label,
        secret=store.decrypt_entry(matches[0], master_key=master_key),
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        messenger=messenger,
    )
    return "", True


async def handle_vault_gemini_decision(
    decision: dict[str, Any],
    *,
    phone: str,
    user_text: str,
    settings_key: str,
    message_record: dict[str, Any] | None,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
    messenger: StepMessenger | None = None,
) -> tuple[str, bool, bool]:
    """
    Executa vault_save / vault_retrieve / vault_list decidido pelo Gemini.
    Retorna (texto_resposta, redact_user, redact_assistant).
    """
    action = str(decision.get("action") or "").strip().lower()
    store = get_vault_store(phone)
    master_key = vault_master_key(settings_key)
    gemini_reply = str(decision.get("response") or "").strip()

    if not vault_configured(settings_key):
        store.clear_pending()
        return _NOT_CONFIGURED_MSG, False, False

    if action == "vault_list":
        return _format_list_labels(store.list_labels()), False, False

    if action == "vault_save":
        label, secret = _vault_fields(decision)
        if label and secret:
            try:
                msg = await _save_vault_entry(
                    store=store,
                    master_key=master_key,
                    label=label,
                    secret=secret,
                    user_text=user_text,
                    record=message_record,
                    evo=evo,
                    evo_base=evo_base,
                    evo_key=evo_key,
                    instance=instance,
                )
                return msg, True, False
            except ValueError as e:
                return f"Não foi possível guardar: {e}", False, False
        if label and not secret:
            store.save_pending(
                VaultPending(mode="save", stage="secret", label=label)
            )
            return gemini_reply or _SAVE_SECRET_PROMPT, False, False
        if secret and not label:
            store.save_pending(
                VaultPending(mode="save", stage="label", secret_prefill=secret)
            )
            return gemini_reply or _SAVE_LABEL_PROMPT, False, False
        store.save_pending(VaultPending(mode="save", stage="label"))
        return gemini_reply or _SAVE_LABEL_PROMPT, False, False

    if action == "vault_retrieve":
        label_query, _ = _vault_fields(decision)
        msg, redact_asst = await _handle_vault_retrieve(
            phone=phone,
            label_query=label_query,
            store=store,
            master_key=master_key,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            messenger=messenger,
        )
        if msg:
            return msg, False, redact_asst
        return gemini_reply or "Senha enviada.", False, True

    return "Não entendi o pedido sobre o cofre de senhas.", False, False


async def try_handle_vault_intent_direct(
    phone: str,
    user_text: str,
    *,
    settings: Any,
    record: dict[str, Any] | None,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    """
    Trata pedidos explícitos ao cofre (listar/recuperar/guardar) sem passar pelo Gemini.
    Evita que senhas na memória pessoal sejam devolvidas via action=reply.
    """
    action, label = classify_vault_intent(user_text)
    if not action:
        return False

    if not evo_base or not evo_key or not instance:
        log.error("Cofre senhas direto sem Evolution phone=%s", phone)
        return True

    messenger = StepMessenger(
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        phone=phone,
    )
    decision: dict[str, Any] = {"action": action}
    if label:
        decision["vault_label"] = label

    redact_user = False
    redact_assistant = False
    reply = ""

    async with TypingSession(
        evo, evo_base=evo_base, evo_key=evo_key, instance=instance, phone=phone
    ):
        settings_key = getattr(settings, "vault_master_key", "")
        reply, redact_user, redact_assistant = await handle_vault_gemini_decision(
            decision,
            phone=phone,
            user_text=user_text,
            settings_key=settings_key,
            message_record=record,
            evo=evo,
            evo_base=evo_base,
            evo_key=evo_key,
            instance=instance,
            messenger=messenger,
        )
        if reply and not messenger.sent_any:
            await messenger.step(reply, final=True)

    await _finish_exchange(
        phone=phone,
        user_text=user_text,
        messenger=messenger,
        reply_text=reply,
        evo=evo,
        evo_base=evo_base,
        evo_key=evo_key,
        instance=instance,
        redact_user=redact_user,
        redact_assistant=redact_assistant,
    )
    log.info("Cofre senhas (direto) phone=%s action=%s", phone, action)
    return True


async def try_handle_password_vault_pending(
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
    Continua fluxo multi-passo do cofre (rotulo, senha, escolha na lista).
    Retorna True se havia pendencia e a mensagem foi consumida.
    """
    store = get_vault_store(phone)
    pending = store.load_pending()
    if not pending:
        return False

    text = (user_text or "").strip()
    settings_key = getattr(settings, "vault_master_key", "")
    master_key = vault_master_key(settings_key)

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
        if _CANCEL_RE.match(text):
            store.clear_pending()
            reply = "Ok, cancelado."
        elif not vault_configured(settings_key):
            store.clear_pending()
            reply = _NOT_CONFIGURED_MSG
        elif pending.mode == "save":
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
        elif pending.mode == "retrieve_pick":
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
