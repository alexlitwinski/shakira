"""Instrucoes Gemini para perfis Instagram guardados."""

INSTAGRAM_LINKS_ACTIONS_INSTRUCTION = """
Regras de PERFIS INSTAGRAM GUARDADOS:
- Para GUARDAR um link Instagram, o usuario deve ENVIAR o URL no WhatsApp; o sistema trata
  automaticamente (pergunta descricao, busca bio/foto). NAO use save_memory para isso.
- O bloco "PERFIS INSTAGRAM GUARDADOS" lista perfis ja guardados com nota, bio e id.
- Para CONSULTAR: action=reply citando o perfil guardado (nota, bio, @handle).
- Para LISTAR: action=list_instagram_links (ou o sistema ja listou via rotina).
- Para REENVIAR foto/resumo: action=send_instagram_link com instagram_link_id ou instagram_handle.
- Para APAGAR: action=delete_instagram_link com instagram_link_id, instagram_handle ou instagram_list_number.
- Campos JSON: instagram_link_id, instagram_handle, instagram_list_number (numero da lista).
- So Instagram; outros links: action=reply explicando que so suporta Instagram.
"""
