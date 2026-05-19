"""Prompts para geracao de respostas proactivas (disparo de agendamento)."""

from __future__ import annotations

SCHEDULED_REPLY_SYSTEM = """Voce e o assistente da casa. O usuario recebe mensagens no WhatsApp.
Responda SOMENTE com texto plano em portugues do Brasil — sem JSON, sem markdown, sem entity_id.
Use linguagem simples e natural para pessoas leigas.
Esta e uma mensagem PROATIVA: o sistema disparou um aviso agendado anteriormente.
Informe o usuario de forma curta e amigavel sobre o que aconteceu.
Nao peca confirmacao nem sugira novas acoes a menos que o contexto do agendamento exija."""


def build_scheduled_reply_prompt(
    *,
    context: str,
    trigger_summary: str,
    entity_states_block: str,
    conversation_history: str = "",
) -> str:
    history_block = ""
    if conversation_history.strip():
        history_block = f"Historico recente da conversa:\n{conversation_history.strip()}\n\n"

    states_block = ""
    if entity_states_block.strip():
        states_block = f"Estados actuais relevantes:\n{entity_states_block.strip()}\n\n"

    return f"""{history_block}{states_block}Contexto do agendamento (por que foi criado):
{context.strip()}

Trigger disparado:
{trigger_summary.strip()}

Escreva uma unica mensagem curta para o WhatsApp informando o usuario."""


def build_fallback_message(*, label: str, context: str, trigger_summary: str) -> str:
    headline = label.strip() or context.strip()[:80] or "Aviso agendado"
    return f"{headline}. ({trigger_summary})"
