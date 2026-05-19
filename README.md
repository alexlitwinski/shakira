# Shakira — Assistente WhatsApp para Home Assistant

Add-on que recebe webhooks da **Evolution API**, valida números em `input_text.whatsapp_bot_permitidos`, consulta **todas** as entidades do Home Assistant e usa o **Gemini** para responder. **Alterações** só em entidades definidas em `/config/shakira_devices.yaml`.

## Layout do repositório

Os ficheiros do add-on estão em [`shakira/`](shakira/). Na loja: **`https://github.com/alexlitwinski/shakira`**

---

## Consultar vs agir

| Tipo | O que pode fazer |
|------|------------------|
| **Consultar** | Qualquer entidade do HA (estados enviados a cada mensagem) |
| **Agir** (`call_service`) | Apenas entidades com `allow_actions: true` em `shakira_devices.yaml` |

O **catálogo de dispositivos** fica em **cache de contexto Gemini** (não é reenviado a cada WhatsApp). A cada mensagem só vão os **estados atuais** + o texto do utilizador.

---

## Memória por utilizador (WhatsApp)

Cada número autorizado tem memória persistente em **`/data/shakira_users/<telefone>/`** (volume do add-on):

| Ficheiro / pasta | Conteúdo |
|------------------|----------|
| `memories.json` | Notas e factos que o utilizador pediu para guardar |
| `files/` | Imagens, PDFs e documentos enviados pelo WhatsApp |
| `files_manifest.json` | Índice dos ficheiros (id, nome, tipo, legenda) |

**Exemplos de pedidos**

- *"Lembra que o código do portão é 4521"* — guarda texto na memória.
- *"O que eu te pedi para lembrar?"* — resposta com base na memória persistente.
- Enviar um PDF com *"guarda isto"* — ficheiro gravado em disco.
- *"Manda o PDF que guardei"* — reenvio do ficheiro pelo WhatsApp.
- Enviar ficheiro **sem mensagem** — o assistente pergunta: memória pessoal (ex.: convite de show) ou **PhotoPrism** (só fotos; pode indicar álbum, ex.: *PhotoPrism álbum Viagens*).

Quando a memória de um utilizador é grande (vários KB de texto), o add-on pode criar um **cache Gemini dedicado** por número para acelerar a recuperação. Limites configuráveis: `SHAKIRA_MAX_MEMORIES_PER_USER`, `SHAKIRA_MAX_FILES_PER_USER`, `SHAKIRA_MAX_FILE_BYTES`.

---

## Arquivo de dispositivos (`/config/shakira_devices.yaml`)

Copie o exemplo [`shakira/shakira_devices.example.yaml`](shakira/shakira_devices.example.yaml) para **`/config/shakira_devices.yaml`** no Home Assistant.

```yaml
devices:
  - name: Boiler
    entities:
      - entity_id: sensor.temperatura_boiler
        description: Informa a temperatura atual do boiler
        allow_actions: false

      - entity_id: switch.disjuntor_boiler_interruptor
        description: Aquecimento eletrico do boiler
        allow_actions: true

  - name: Porta social
    entities:
      - entity_id: lock.porta_social
        description: Porta social - trancar ou destrancar
        allow_actions: true
        security:
          require_password_for_services:
            - unlock
          password: "1234"
          password_prompt: "Qual a senha para destrancar a porta social?"
```

- `allow_actions: false` — só contexto (o bot explica, mas não altera).
- `allow_actions: true` — pode executar serviços HA nessa entidade.
- `security` — exige senha antes de `unlock` (ou outros serviços listados).

Ao editar o YAML, o cache Gemini é recriado na próxima mensagem.

---

## Alertas periódicos (`/config/shakira_alerts.yaml`)

Copie o exemplo [`shakira/shakira_alerts.example.yaml`](shakira/shakira_alerts.example.yaml) para **`/config/shakira_alerts.yaml`**.

O add-on verifica cada regra no intervalo configurado. Se o estado da entidade coincidir com `when_state`, envia a mensagem via WhatsApp (Evolution API).

```yaml
alerts:
  - id: cameras_paradas
    enabled: true
    check_interval: 5m          # ou check_interval_seconds: 300
    entity_id: binary_sensor.status_cameras_paradas
    when_state: "on"
    message: "Atenção: existem câmeras do sistema com problema."
    cooldown: 1h                # evita repetir o aviso enquanto continuar "on"
    notify:
      phones: []                # vazio = números em input_text.whatsapp_bot_permitidos
```

- **check_interval** — periodicidade da verificação (`30s`, `5m`, `1h`; mínimo 60s).
- **cooldown** — tempo mínimo entre avisos da mesma regra enquanto a condição permanece ativa.
- **notify.phones** — lista opcional de destinos (DDI+DDD+número, só dígitos).

Também é possível editar o arquivo na aba **shakira_alerts.yaml** do painel Ingress do add-on.

---

## Opções do add-on

| Opção | Descrição |
|-------|-----------|
| **ha_url** | `http://supervisor/core` (padrão HA OS) |
| **homeassistant_long_lived_token** | Token de longa duração (Perfil HA) se `SUPERVISOR_TOKEN` falhar |
| **evolution_base_url** / **evolution_api_key** / **evolution_instance** | Evolution API |
| **gemini_api_key** | Google AI Studio |
| **devices_config_path** | Caminho do YAML (padrão `/config/shakira_devices.yaml`) |
| **alerts_config_path** | Caminho do YAML de alertas (padrão `/config/shakira_alerts.yaml`) |
| **gemini_cache_ttl_hours** | TTL do cache Gemini em horas (padrão 24) |

---

## Entidade HA obrigatória

| Entidade | Uso |
|----------|-----|
| `input_text.whatsapp_bot_permitidos` | Números permitidos (só dígitos, separados por vírgula) |

---

## Evolution + Nabu Casa (recomendado)

1. Webhook na Evolution: `https://<id>.ui.nabu.casa/api/webhook/<seu_webhook_id>`
2. Automação HA com `local_only: false` e `rest_command` para `http://<IP_HA>:8098/webhook` (porta host do add-on).

Eventos: **`MESSAGES_UPSERT`**.

---

## Instalação

1. Repositório público na loja de add-ons.
2. Instalar **Shakira** (versão **1.0.3+**), preencher opções, **Rebuild** se necessário.
3. Criar `/config/shakira_devices.yaml`.
4. Reiniciar o add-on.

---

## Variáveis de ambiente (dev)

`HA_URL`, `HOMEASSISTANT_TOKEN`, `GEMINI_API_KEY`, `SHAKIRA_DEVICES_PATH`, `GEMINI_MODEL`, `ENTITY_CONTEXT_MAX_CHARS`.
