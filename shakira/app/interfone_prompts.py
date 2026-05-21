"""Instrucoes Gemini para historico de chamadas do interfone."""

INTERFONE_ACTIONS_INSTRUCTION = """
Regras de CHAMADAS DO INTERFONE (historico guardado automaticamente):
- Quando o usuario perguntar sobre chamadas do interfone, interfone, porteiro, campainha,
  "quem tocou", "ultimas chamadas", "historico do interfone", use action=interfone_list.
- Campo opcional interfone_list_limit: quantas chamadas mostrar (1-15, padrao 5).
- NAO invente chamadas — o sistema envia fotos e dados reais guardados na casa.
- Se o usuario pedir detalhe de uma chamada especifica sem numero, use interfone_list com limite 5.
- Para abrir portao ou atender agora, use as acoes normais de casa (call_service / cenarios), nao interfone_list.
"""
