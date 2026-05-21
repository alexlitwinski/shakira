"""Instrucoes Gemini para Google Calendar (link publico por usuario)."""

GOOGLE_CALENDAR_ACTIONS_INSTRUCTION = """
Regras de AGENDA GOOGLE (link publico por usuario):
- O bloco "Agenda Google" no contexto indica se o link publico ja foi guardado e as preferencias atuais.
- Se NAO houver link configurado e o usuario pedir agenda/compromissos/alertas/resumo, action=reply pedindo
  o link publico do Google Calendar (Integrar calendario > endereco publico com cid=).
- Se o usuario ENVIAR um link calendar.google.com na mensagem, action=google_calendar_save_link
  (NUNCA action=reply dizendo que recebeu o link sem executar google_calendar_save_link).
- Para GUARDAR o link publico: action=google_calendar_save_link com calendar_public_url.
- Para CONSULTAR compromissos: action=google_calendar_list_events (calendar_list_days 1-14 ou calendar_list_date YYYY-MM-DD).
- Para VER ou ALTERAR preferencias (antecedencia dos alertas, horario do resumo, fuso): action=google_calendar_configure
  ou google_calendar_show_settings.
- calendar_alert_advance_minutes: minutos de antecedencia dos lembretes (ex.: 15, 30, 60).
- calendar_daily_summary_time: horario HH:MM do resumo diario (ex.: "07:00", "08:30").
- calendar_timezone: fuso IANA opcional (padrao America/Sao_Paulo).
- calendar_alerts_enabled / calendar_daily_summary_enabled: true|false para ligar/desligar.
- Interprete a INTENCAO ("me avise 15 min antes", "resumo as 8h", "o que tenho amanha") — sem depender de palavras fixas.
- NUNCA invente eventos; google_calendar_list_events consulta o feed ICS real.
- Campos JSON: calendar_public_url, calendar_alert_advance_minutes, calendar_daily_summary_time,
  calendar_timezone, calendar_alerts_enabled, calendar_daily_summary_enabled, calendar_list_days, calendar_list_date.
- Acoes validas: google_calendar_save_link | google_calendar_configure | google_calendar_list_events | google_calendar_show_settings.
"""
