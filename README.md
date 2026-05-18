# Shakira — Assistente WhatsApp para Home Assistant

Add-on que recebe webhooks da **Evolution API**, valida números em `input_text.whatsapp_bot_permitidos`, consulta todas as entidades via API do Home Assistant e usa o **Gemini** para decidir comandos ou respostas.

## Layout do repositório (obrigatório para a loja do HA)

O Home Assistant só aceita repositórios em que **cada add-on é uma subpasta** com `config.yaml` e `Dockerfile` dentro — não na raiz do GitHub.

Este repo está assim:

- [`repository.yaml`](repository.yaml) — metadados do repositório (opcional mas recomendado)
- **`shakira/`** — pasta do add-on (nome alinhado ao `slug` em `config.yaml`)
  - código Python em `shakira/app/`, Docker, etc.

Ao adicionar a loja, use apenas: **`https://github.com/alexlitwinski/shakira`** (sem subpath).

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

## Execução local (desenvolvimento)

A partir da pasta do add-on:

```bash
cd shakira
pip install -r requirements.txt
set HA_URL=http://localhost:8123
set HOMEASSISTANT_TOKEN=seu_token
python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

## Instalação como repositório de add-ons

1. Confirme que o repositório GitHub está **público** (a loja clona por HTTPS sem credenciais).
2. Em **Settings → Add-ons → Add-on store → ⋮ → Repositories**, adicione **`https://github.com/alexlitwinski/shakira`**.
3. Procure pelo add-on **Shakira** e instale.
4. Configure as entidades `input_text` e o webhook na Evolution.

### Erro «not a valid add-on repository»

Quase sempre significa estrutura errada na raiz. Tem de existir uma subpasta (aqui **`shakira/`**) contendo pelo menos **`config.yaml`** e **`Dockerfile`**.

### Erro ao clonar (pedir usuário/password)

Repositório **privado** — torne público ou use instalação local copiando a pasta `shakira/` para `/addons`.

## Observações

- O filtro `/webhook` ignora eventos que não contenham **`UPSERT`** no nome.
- Conversas em **grupo** são ignoradas; apenas chats privados.
- Lista grande de entidades pode ser truncada pelo limite de caracteres (configurável).
