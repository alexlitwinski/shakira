"""Instrucoes de memoria por usuario (fora do cache global de catalogo)."""

USER_MEMORY_ACTIONS_INSTRUCTION = """
MEMORIA E ARQUIVOS DO USUARIO:
- O usuario pode pedir para GUARDAR, LEMBRAR ou ANOTAR informacoes (ex.: "lembra que minha senha do wifi e X").
- O usuario pode pedir para RECUPERAR o que foi guardado (ex.: "o que eu te pedi para lembrar?", "qual era aquela nota?").
- O usuario pode enviar arquivos (foto, PDF, documento) pedindo para guardar; o sistema salva automaticamente.
- Use as memorias listadas em "Memoria persistente" abaixo para responder recuperacoes.
- Para guardar texto novo: action=save_memory com memory_text (e opcional memory_label).
- Para reenviar arquivo guardado: action=send_user_file com file_id ou file_name (nome do arquivo).
- Nao use save_memory para comandos de casa (luzes, fechaduras, etc.).

Campos adicionais no JSON:
  "memory_text": "texto a guardar na memoria persistente",
  "memory_label": "rotulo curto opcional (ex.: wifi, aniversario)",
  "file_id": "id do arquivo guardado",
  "file_name": "nome do arquivo guardado (alternativa ao file_id)"

Novas acoes validas para "action":
  "save_memory" | "send_user_file"

Exemplos:
- "Lembra que o codigo da porta e 4521" -> save_memory, memory_text="codigo da porta e 4521", response confirmando
- "O que eu te pedi para lembrar do wifi?" -> reply usando a memoria persistente
- "Manda aquele PDF que guardei" -> send_user_file com file_name ou file_id, response curta
"""
