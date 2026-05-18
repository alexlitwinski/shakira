# Shakira — Assistente WhatsApp para Home Assistant

Add-on que recebe webhooks da **Evolution API**, valida números vindos apenas de `input_text.whatsapp_bot_permitidos`, consulta entidades pela API REST do Home Assistant e usa o **Gemini** (chave configurada no add-on) para comandos ou respostas.

## Layout do repositório (Home Assistant Supervisor)

Cada **app** deve estar numa subpasta. Os ficheiros do add-on ficam em [`shakira/`](shakira/).

Na loja utilize: **`https://github.com/alexlitwinski/shakira`**

### Build da imagem (Supervisor ≥ 2026.04)

Usa **`FROM ghcr.io/home-assistant/base:latest`** (sem `build.yaml`). O erro `amd64-base-python:3.12: not found` vinha das imagens Python antigas; o fluxo atual instala Python 3 na base Alpine oficial.

---

## Opções do add-on (Configuração)

| Opção | Descrição |
|-------|-----------|
| **ha_url** | URL da API Core (HA OS típico: `http://supervisor/core`). |
| **homeassistant_long_lived_token** | Fallback se o Supervisor não definir token: cole um token de **Perfil HA → Tokens de longa duração** (recomenda-se mesmo que o erro `SUPERVISOR_TOKEN ausente` desapareça). |
| **evolution_base_url** | URL base da Evolution (ex: `http://192.168.1.50:8080`), sem `/` final. |
| **evolution_api_key** | Cabeçalho `apikey` da Evolution. |
| **gemini_api_key** | Chave da API Google AI (Gemini). |
| **evolution_instance** | Nome da instância Evolution (opcional se o webhook já enviar `instance`). |

Variáveis de ambiente opcionais (dev): `HA_URL`, `HOMEASSISTANT_TOKEN`, `EVOLUTION_BASE_URL`, `EVOLUTION_API_KEY`, `GEMINI_API_KEY`, `EVOLUTION_INSTANCE`.

---

## Só obtido das entidades do HA

| Entidade | Conteúdo |
|----------|----------|
| `input_text.whatsapp_bot_permitidos` | Telefones **só com dígitos**, separados por vírgula (ex: `553191119016,553198946418`). |

Chaves Gemini/Evolution **não** são mais lidas de `input_text`.

### Log «SUPERVISOR_TOKEN ausente»

Alguns setups não expõem `SUPERVISOR_TOKEN` no container no arranque. **Solução imediata:** na configuração do add-on Shakira preencher **homeassistant_long_lived_token** e guardar; usar **ha_url** `http://supervisor/core`. Depois **reinicia** o Shakira.

---

## Evolution — Webhook

**POST** `http://<IP_HOME_ASSISTANT>:8099/webhook`

Incluir eventos do tipo **`messages.upsert`** (nome do evento que contenha `UPSERT`).

---

## Variáveis de ambiente opcionais

| Variável | Efeito |
|----------|--------|
| `GEMINI_MODEL` | Modelo Gemini (pré-definido `gemini-2.0-flash`). |
| `ENTITY_CONTEXT_MAX_CHARS` | Limite do resumo das entidades (pré-definido `120000`). |

---

## Execução local (`shakira/app`)

```bash
cd shakira
pip install -r requirements.txt
set HA_URL=http://localhost:8123
set HOMEASSISTANT_TOKEN=seu_token
set GEMINI_API_KEY=...
set EVOLUTION_BASE_URL=...
set EVOLUTION_API_KEY=...
python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

(Linux/macOS substitua `set` por `export`.)

---

## Instalação

1. Repo **GitHub público**.
2. **Add-on Store** → **Repositories** → `https://github.com/alexlitwinski/shakira`.
3. Instalar **Shakira**, preencher opções Evolution + Gemini e reiniciar.
4. Garantir a entidade `input_text.whatsapp_bot_permitidos` e configurar o webhook na Evolution.

---

## Observações

- Filtro `/webhook`: só processa payloads cujo campo `event` contenha **`UPSERT`** (ou está vazio).
- Grupos `@g.us` são ignorados.
- Listas grandes de entidades podem ser truncadas.
