"""Consulta do historico de chamadas do interfone via WhatsApp."""

from __future__ import annotations

import logging
from typing import Any

from app.config import AppSettings
from app.evolution import EvolutionClient
from app.interfone_call_store import (
    InterfoneCallRecord,
    format_local_datetime,
    get_interfone_store,
)
from app.whatsapp_steps import StepMessenger, pulse_whatsapp_typing
from app.whatsapp_steps import truncate_whatsapp

log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 5
MAX_LIST_LIMIT = 15


def _parse_limit(decision: dict[str, Any]) -> int:
    raw = decision.get("interfone_list_limit")
    if isinstance(raw, int) and not isinstance(raw, bool):
        n = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        n = int(raw.strip())
    else:
        n = DEFAULT_LIST_LIMIT
    return max(1, min(n, MAX_LIST_LIMIT))


def _format_call_caption(record: InterfoneCallRecord, *, index: int, total: int) -> str:
    when = format_local_datetime(record.started_at)
    summary = truncate_whatsapp(record.gemini_summary or "Sem descrição do visitante.", 500)
    status = record.attended_label()
    lines = [
        f"Chamada {index}/{total} — {when}",
        summary,
        f"*{status}*",
    ]
    if record.finalized_at:
        lines.append(record.attend_details())
    return truncate_whatsapp("\n".join(lines), 1024)


async def handle_interfone_list(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    phone: str,
    instance: str,
    messenger: StepMessenger | None = None,
    data_path: str = "",
) -> str:
    """Envia historico recente com imagem, data/hora, resumo Gemini e atendimento."""
    limit = _parse_limit(decision)
    store = get_interfone_store(data_path or None)
    calls = store.list_calls(limit=limit)

    if not calls:
        msg = "Ainda não há chamadas do interfone registadas."
        if messenger:
            await messenger.step(msg, final=True)
            return False
        return msg

    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    if not evo_base or not evo_key or not instance:
        msg = "Não consigo enviar o histórico: Evolution não configurado."
        if messenger:
            await messenger.step(msg, final=True)
            return False
        return msg

    intro = truncate_whatsapp(
        f"Últimas {len(calls)} chamada(s) do interfone:"
    )
    if messenger:
        await messenger.step(intro)
    else:
        await pulse_whatsapp_typing()
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=intro,
        )

    total = len(calls)
    sent = 0
    for index, record in enumerate(calls, start=1):
        caption = _format_call_caption(record, index=index, total=total)
        image_path = store.image_path(record)
        await pulse_whatsapp_typing()

        if image_path:
            image_bytes = image_path.read_bytes()
            ok = await evo.send_image_bytes(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                image_bytes=image_bytes,
                filename=record.image_file or f"interfone_{record.id}.jpg",
                caption=caption,
            )
            if ok is not None:
                sent += 1
            else:
                log.warning("Falha ao enviar imagem interfone id=%s", record.id)
                text_fallback = f"{caption}\n_(imagem indisponível)_"
                if messenger:
                    await messenger.step(text_fallback)
                else:
                    await evo.send_text(
                        base_url=evo_base,
                        api_key=evo_key,
                        instance=instance,
                        number=phone,
                        text=text_fallback,
                    )
        else:
            if messenger:
                await messenger.step(caption)
            else:
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    text=caption,
                )
            sent += 1

    if sent == 0:
        msg = "Encontrei chamadas registadas mas não consegui enviá-las."
        if messenger:
            await messenger.step(msg, final=True)
            return False
        return msg

    if messenger:
        return True
    return f"Enviei {sent} chamada(s) do interfone."
