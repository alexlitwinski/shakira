"""Instrucoes Gemini para perfis Instagram guardados."""

INSTAGRAM_LINKS_ACTIONS_INSTRUCTION = """
Regras de PERFIS INSTAGRAM GUARDADOS:
- Para GUARDAR um link Instagram, o usuario deve ENVIAR o URL no WhatsApp; o sistema trata
  automaticamente (pergunta descricao, busca bio/foto). NAO use save_memory para isso.
- O bloco "PERFIS INSTAGRAM GUARDADOS" (contexto interno) lista nota, bio, @handle e id.
  NUNCA mostre id ao usuario nas respostas.
- BUSCAR perfil guardado por tema ("perfil sobre IA", "qual fala de medicina"): action=search_instagram_links
  com instagram_search_query — NUNCA use search_photos para isso.
- Se o historico recente fala de perfis Instagram e o usuario pede algo por tema, e perfil Instagram,
  nao foto: search_instagram_links ou reply com os perfis do contexto.
- Para LISTAR todos: action=list_instagram_links.
- Para REENVIAR foto/resumo: action=send_instagram_link com instagram_handle ou instagram_list_number.
- Para ATUALIZAR bio/foto via Apify ("atualiza o perfil @x"): action=refresh_instagram_link
  com instagram_handle ou instagram_list_number; response curta confirmando.
- Para APAGAR: action=delete_instagram_link com instagram_handle ou instagram_list_number.
- Campos JSON: instagram_link_id (so uso interno), instagram_handle, instagram_list_number,
  instagram_search_query.
- So Instagram; outros links: action=reply explicando que so suporta Instagram.
"""
