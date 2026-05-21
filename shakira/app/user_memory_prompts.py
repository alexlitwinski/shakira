"""Instruções de memória por usuário (fora do cache global de catálogo)."""

USER_MEMORY_ACTIONS_INSTRUCTION = """
MEMÓRIA E ARQUIVOS DO USUÁRIO:
- O usuário pode pedir para GUARDAR, LEMBRAR ou ANOTAR informações (ex.: "lembra que minha senha do wifi é X").
- O usuário pode pedir para RECUPERAR o que foi guardado (ex.: "o que eu te pedi para lembrar?", "qual era aquela nota?").
- O usuário pode pedir para APAGAR anotação ou arquivo (ex.: "apaga ele", "remove da memória", "exclui aquele arquivo").
- O usuário pode enviar arquivos (foto, PDF, documento) pedindo para guardar; o sistema salva automaticamente.
- Use as memórias listadas em "Memória persistente" abaixo para responder recuperações.
- Se o usuário pedir para VER o que está guardado (lista do registro pessoal), o sistema responde
  automaticamente com os 20 itens mais recentes e a contagem dos restantes — não liste tudo no JSON.
- Para guardar texto novo (exceto credenciais): action=save_memory com memory_text (e opcional memory_label).
- Para GUARDAR senhas, PINs, códigos de contas/sites/Wi-Fi: action=vault_save com vault_label e vault_secret — NUNCA save_memory.
- Para reenviar arquivo guardado: action=send_user_file com file_id ou file_name.
- Para apagar: action=delete_from_memory com memory_id (anotação) ou file_id/file_name (arquivo).
- Registro pessoal: antes de guardar arquivo, confirme que sabe do que se trata; senão peça descrição curta ao usuário.
- NUNCA use send_user_file quando o usuário pedir apagar, excluir ou remover.
- Não use save_memory para comandos de casa (luzes, fechaduras, etc.).
- Não use save_memory para senhas de contas/sites — use vault_save, vault_retrieve ou vault_list.

Campos adicionais no JSON:
  "memory_text": "texto a guardar na memória persistente",
  "memory_label": "rótulo curto opcional (ex.: wifi, aniversário)",
  "memory_id": "id da anotação a apagar",
  "file_id": "id do arquivo guardado",
  "file_name": "nome do arquivo guardado (alternativa ao file_id)"

Novas ações válidas para "action":
  "save_memory" | "send_user_file" | "delete_from_memory"

Exemplos:
- "Lembra que a senha do wifi é abc123" -> vault_save, vault_label="wifi", vault_secret="abc123", response confirmando
- "O que eu te pedi para lembrar do wifi?" -> vault_retrieve com vault_label="wifi" (ou reply se for fato sem credencial)
- "Manda aquele PDF que guardei" -> send_user_file com file_name ou file_id, response curta
- "Apague ele" (após citar um arquivo) -> delete_from_memory com file_id do item mencionado
"""
