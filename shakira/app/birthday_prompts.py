"""Instrucoes Gemini para aniversarios guardados."""

BIRTHDAY_ACTIONS_INSTRUCTION = """
Regras de ANIVERSARIOS GUARDADOS:
- Quando o usuario informar nome + data de aniversario (ex.: "Maria nasceu dia 15/03",
  "aniversario do Joao e 20 de dezembro", "guarda: Ana 05/08/1990"), use action=birthday_save.
- Campos: birthday_name, birthday_day (1-31), birthday_month (1-12), birthday_year (opcional),
  birthday_note (opcional). Alternativa: birthday_date em DD/MM ou DD/MM/YYYY ou "15 de marco".
- NAO use save_memory para aniversarios — use sempre birthday_save no ficheiro dedicado.
- Para LISTAR todos: action=birthday_list.
- Para ver PROXIMOS (esta semana, proximos dias): action=birthday_upcoming
  (birthday_upcoming_days opcional, padrao 7).
- Para APAGAR: action=birthday_delete com birthday_name, birthday_list_number ou birthday_id.
- O bloco "ANIVERSARIOS GUARDADOS" no contexto lista nome, data e id interno — nunca mostre id ao usuario.
- Interprete a intencao livremente ("quem faz anos essa semana?", "lista aniversarios").
- Se faltar nome ou data, action=reply pedindo o que falta.
- Varios aniversarios na MESMA mensagem: responda com JSON ARRAY de objetos birthday_save
  (um por pessoa), NAO envie o array como texto em "response" ao usuario.
  Ex.: [{"action":"birthday_save","birthday_name":"Ana",...},{"action":"birthday_save",...}]
"""
