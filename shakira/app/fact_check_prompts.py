"""Instrucoes Gemini para verificacao de noticias (Google Fact Check Tools)."""

FACT_CHECK_ACTIONS_INSTRUCTION = """
Regras de VERIFICACAO DE NOTICIAS / FACT-CHECK (fact_check_claim):
- Use APENAS quando o usuario pedir explicitamente para VERIFICAR, CHECAR, CONFIRMAR ou DESMENTIR a veracidade de uma noticia, alegacao, boato, informacao viral, "e verdade?", "e fake?", "isso procede?", etc. O pedido deve conter palavras-chave claras de fact-checking ou veracidade de noticias.
- NUNCA use para comandos ou checagens fisicas da casa/dispositivos (ex.: "verifique a rua", "verifique as cameras", "verifique o boiler", "veja a porta"). Pedidos de verificacao fisica ou status da casa pertencem a get_camera_snapshot, house_status, get_state ou reply, NUNCA a fact_check_claim.
- action=fact_check_claim com fact_check_query preenchido com a alegacao ou tema a verificar (frase clara, em portugues, com os termos principais da noticia).
- fact_check_language: opcional, BCP-47 (padrao pt-BR). So altere se o usuario pedir outro idioma.
- response: mensagem curta antes da busca (ex.: "Vou consultar verificadores de fact-check...").
- NUNCA invente veredito nem cite fontes sem o sistema consultar a API — o resultado vem da rotina.
- Se faltar contexto (alegacao vaga), action=reply pedindo o trecho ou link da noticia.
- Nao use fact_check_claim para consultar sensores/dispositivos da casa, senhas, fotos ou Instagram.
- Nao confunda com reply generico: se o pedido e checar veracidade de informacao externa, e fact_check_claim.
- PROIBIDO recusar por ser "automacao residencial" ou "sem conhecimento medico/juridico" se o pedido for de fato um boato/noticia viral — use fact_check_claim.
- Campos JSON: fact_check_query (obrigatorio), fact_check_language (opcional).
"""
