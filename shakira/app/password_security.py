"""Apaga mensagens com senha no WhatsApp apos confirmacao (seguranca)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.evolution import EvolutionClient

log = logging.getLogger(__name__)

SECURITY_DELETE_NOTICE = (
    "Apaguei a sua mensagem com a senha no WhatsApp por segurança."
)

_CANCEL_RE = re.compile(
    r"^\s*(cancelar|cancela|nao|não|desistir)\s*[.!?]?\s*$",
    re.I,
)


def is_security_cancel(user_text: str) -> bool:
    return bool(_CANCEL_RE.match((user_text or "").strip()))


def extract_message_delete_key(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extrai id/remoteJid/fromMe do webhook Evolution para apagar a mensagem."""
    key = record.get("key") or {}
    if not isinstance(key, dict):
        key = {}
    msg_id = key.get("id") or record.get("messageId") or record.get("id")
    remote_jid = key.get("remoteJid") or record.get("remoteJid")
    if not msg_id or not remote_jid:
        return None
    from_me = key.get("fromMe")
    if from_me is None:
        from_me = record.get("fromMe")
    out: dict[str, Any] = {
        "id": str(msg_id),
        "remoteJid": str(remote_jid),
        "fromMe": bool(from_me),
    }
    participant = key.get("participant")
    if isinstance(participant, str) and participant.strip():
        out["participant"] = participant.strip()
    return out


def append_security_delete_notice(reply: str, *, deleted: bool) -> str:
    if not deleted:
        return reply
    notice = SECURITY_DELETE_NOTICE.strip()
    body = (reply or "").strip()
    if not body:
        return notice
    if notice.lower() in body.lower():
        return body
    return f"{body}\n\n{notice}"


async def try_delete_inbound_password_message(
    *,
    record: dict[str, Any] | None,
    user_text: str,
    should_treat_as_password: bool,
    evo: EvolutionClient,
    evo_base: str,
    evo_key: str,
    instance: str,
) -> bool:
    """Apaga a mensagem recebida que continha senha. Retorna True se apagou."""
    if not should_treat_as_password or is_security_cancel(user_text):
        return False
    if not evo_base or not evo_key or not instance:
        return False
    if not isinstance(record, dict):
        log.warning("Sem record Evolution para apagar mensagem com senha")
        return False
    delete_key = extract_message_delete_key(record)
    if not delete_key:
        log.warning("Chave da mensagem incompleta para apagar senha")
        return False
    ok = await evo.delete_message_for_everyone(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        message_id=str(delete_key["id"]),
        remote_jid=str(delete_key["remoteJid"]),
        from_me=bool(delete_key.get("fromMe")),
        participant=(
            str(delete_key["participant"])
            if delete_key.get("participant")
            else None
        ),
    )
    if ok:
        log.info(
            "Mensagem com senha apagada no WhatsApp id=%s remoteJid=%s",
            delete_key["id"],
            delete_key["remoteJid"],
        )
    return ok
