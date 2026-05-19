"""Instrucoes de memoria por usuario (fora do cache global de catalogo)."""

USER_MEMORY_ACTIONS_INSTRUCTION = """
MEMORIA E ARQUIVOS DO USUARIO:
- O usuario pode pedir para GUARDAR, LEMBRAR ou ANOTAR informacoes (ex.: "lembra que minha senha do wifi e X").
- O usuario pode pedir para RECUPERAR o que foi guardado (ex.: "o que eu te pedi para lembrar?", "qual era aquela nota?").
- O usuario pode pedir para APAGAR anotacao ou arquivo (ex.: "apaga ele", "remove da memoria", "exclui aquele arquivo").
- O usuario pode enviar arquivos (foto, PDF, documento) pedindo para guardar; o sistema salva automaticamente.
- Use as memorias listadas em "Memoria persistente" abaixo para responder recuperacoes.
- Para guardar texto novo: action=save_memory com memory_text (e opcional memory_label).
- Para reenviar arquivo guardado: action=send_user_file com file_id ou file_name.
- Para apagar: action=delete_from_memory com memory_id (anotacao) ou file_id/file_name (arquivo).
- Registro pessoal: antes de guardar arquivo, confirme que sabe do que se trata; senao peca descricao curta ao usuario.
- NUNCA use send_user_file quando o usuario pedir apagar, excluir ou remover.
- Nao use save_memory para comandos de casa (luzes, fechaduras, etc.).

Campos adicionais no JSON:
  "memory_text": "texto a guardar na memoria persistente",
  "memory_label": "rotulo curto opcional (ex.: wifi, aniversario)",
  "memory_id": "id da anotacao a apagar",
  "file_id": "id do arquivo guardado",
  "file_name": "nome do arquivo guardado (alternativa ao file_id)"

Novas acoes validas para "action":
  "save_memory" | "send_user_file" | "delete_from_memory"

Exemplos:
- "Lembra que o codigo da porta e 4521" -> save_memory, memory_text="codigo da porta e 4521", response confirmando
- "O que eu te pedi para lembrar do wifi?" -> reply usando a memoria persistente
- "Manda aquele PDF que guardei" -> send_user_file com file_name ou file_id, response curta
- "Apague ele" (apos citar um arquivo) -> delete_from_memory com file_id do item mencionado
"""
