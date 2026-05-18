# Shakira — Assistente WhatsApp para Home Assistant

Add-on que recebe webhooks da **Evolution API**, valida números em `input_text.whatsapp_bot_permitidos`, consulta todas as entidades via API do Home Assistant e usa o **Gemini** para decidir comandos ou respostas.

## Entidades obrigatórias no HA

| Entidade | Conteúdo |
|----------|----------|
| `input_text.whatsapp_bot_permitidos` | Números só com dígitos, separados por vírgula (ex: `553191119016,553198946418`). |
| `input_text.integracao_evolution` | URL base da Evolution (ex: `http://evo:8080`). Opcional: `URL|nome_instancia` se a instância não vier no webhook. |
| `input_text.api_key_evolution` | Chave `apikey` da Evolution. |
| `input_text.api_key_gemini` | API key do Google AI Studio para o Gemini. |

## Opções do add-on

- **ha_url**: em instalações normais Supervisor, usar `http://supervisor/core` (padrão).
- **evolution_instance**: nome da instância Evolution, se não estiver codificado como `URL|instancia` em `input_text.integracao_evolution` nem no campo `instance` do webhook.

## Evolution — Webhook

Configure o webhook da instância para **POST**:

`http://<IP_HOME_ASSISTANT>:8099/webhook`

Eventos: inclua **`messages.upsert`** (ou equivalente que contenha “UPSERT” no nome).

## Variáveis de ambiente opcionais (teste local)

- `HA_URL`: URL da API do HA (ex: `http://localhost:8123`).
- `HOMEASSISTANT_TOKEN`: Long-Lived Access Token (substitui o `SUPERVISOR_TOKEN` fora do add-on).
- `OPTIONS_PATH`: caminho para um `options.json` de desenvolvimento.
- `GEMINI_MODEL`: modelo Gemini (padrão `gemini-2.0-flash`).
- `ENTITY_CONTEXT_MAX_CHARS`: limite do resumo enviado ao modelo (padrão `120000`).

## Execução local

```bash
pip install -r requirements.txt
set HA_URL=http://localhost:8123
set HOMEASSISTANT_TOKEN=seu_token
python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

## Instalação como repositório de add-ons

1. Adicione este repositório em **Settings → Add-ons → Add-on store → ⋮ → Repositories**.
2. Instale o add-on **Shakira**.
3. Garanta que as entidades `input_text` existam e configure o webhook na Evolution.

## Observações

- O filtro `/webhook` ignora eventos que não contenham **`UPSERT`** no nome (para evitar ruído de connection update).
- Conversas em **grupo** são ignoradas; apenas chats privados (JID `@s.whatsapp.net`).
- Lista grande de entidades pode ser truncada pelo limite de caracteres (configurável).
