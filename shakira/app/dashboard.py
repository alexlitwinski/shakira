"""Pagina HTML do painel Ingress (status + entidades HA + editores YAML)."""

from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Shakira</title>
  <style>
    :root {
      --bg: #0f1419;
      --card: #1a2332;
      --border: #2d3a4d;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --ok: #3dd68c;
      --warn: #f5c542;
      --err: #f87171;
      --off: #6b7280;
      --accent: #25d366;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      min-height: 100vh;
    }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 1.25rem 1rem 2rem; }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 1rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border);
    }
    h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
    h1 span { color: var(--accent); }
    .meta { font-size: 0.85rem; color: var(--muted); }
    .tabs {
      display: flex;
      gap: 0.35rem;
      margin-bottom: 1rem;
    }
    .tab {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 0.5rem 1rem;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.9rem;
    }
    .tab.active {
      color: var(--text);
      border-color: var(--accent);
      background: rgba(37, 211, 102, 0.08);
    }
    .panel { display: none; }
    .panel.active { display: block; }
    .badge-overall {
      padding: 0.35rem 0.75rem;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-overall.ok { background: rgba(61,214,140,.15); color: var(--ok); }
    .badge-overall.warning { background: rgba(245,197,66,.15); color: var(--warn); }
    .badge-overall.error { background: rgba(248,113,113,.15); color: var(--err); }
    .badge-overall.loading { background: rgba(107,114,128,.2); color: var(--muted); }
    .toolbar { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-bottom: 1rem; }
    button, .btn {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.45rem 0.9rem;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.9rem;
    }
    button:hover { border-color: var(--accent); }
    button.primary {
      background: rgba(37, 211, 102, 0.15);
      border-color: var(--accent);
      color: var(--accent);
      font-weight: 600;
    }
    .grid { display: grid; gap: 0.85rem; }
    @media (min-width: 640px) { .grid { grid-template-columns: 1fr 1fr; } }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.1rem;
      border-left: 4px solid var(--off);
    }
    .card.ok { border-left-color: var(--ok); }
    .card.warning { border-left-color: var(--warn); }
    .card.error { border-left-color: var(--err); }
    .card.disabled { border-left-color: var(--off); }
    .card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; margin-bottom: 0.35rem; }
    .card h2 { margin: 0; font-size: 1rem; font-weight: 600; }
    .pill { font-size: 0.7rem; font-weight: 600; text-transform: uppercase; padding: 0.2rem 0.5rem; border-radius: 6px; }
    .pill.ok { background: rgba(61,214,140,.2); color: var(--ok); }
    .pill.warning { background: rgba(245,197,66,.2); color: var(--warn); }
    .pill.error { background: rgba(248,113,113,.2); color: var(--err); }
    .pill.disabled { background: rgba(107,114,128,.25); color: var(--muted); }
    .summary { color: var(--muted); font-size: 0.9rem; margin: 0 0 0.6rem; }
    details { font-size: 0.8rem; }
    details summary { cursor: pointer; color: var(--accent); }
    pre {
      margin: 0.5rem 0 0;
      padding: 0.6rem;
      background: #0a0e14;
      border-radius: 8px;
      font-size: 0.75rem;
      color: var(--muted);
      white-space: pre-wrap;
      word-break: break-word;
    }
    .yaml-editor {
      width: 100%;
      min-height: 420px;
      font-family: "Cascadia Code", "Consolas", monospace;
      font-size: 0.82rem;
      line-height: 1.45;
      padding: 0.75rem;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #0a0e14;
      color: var(--text);
      resize: vertical;
      tab-size: 2;
    }
    .editor-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem;
    }
    .msg {
      display: none;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
    }
    .msg.error { display: block; background: rgba(248,113,113,.1); border: 1px solid var(--err); color: var(--err); white-space: pre-wrap; }
    .msg.ok { display: block; background: rgba(61,214,140,.1); border: 1px solid var(--ok); color: var(--ok); white-space: pre-wrap; }
    .footer { margin-top: 1.5rem; font-size: 0.8rem; color: var(--muted); text-align: center; }
    .section-title {
      font-size: 1rem;
      font-weight: 600;
      margin: 1.25rem 0 0.65rem;
      color: var(--text);
    }
    .pending-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem;
      margin-top: 0.5rem;
      overflow-x: auto;
    }
    .pending-empty {
      color: var(--muted);
      font-size: 0.9rem;
      margin: 0;
    }
    .pending-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    .pending-table th,
    .pending-table td {
      text-align: left;
      padding: 0.55rem 0.65rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    .pending-table th {
      color: var(--muted);
      font-weight: 600;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .pending-table tr:last-child td { border-bottom: none; }
    .pending-table .mono {
      font-family: "Cascadia Code", "Consolas", monospace;
      font-size: 0.78rem;
      color: var(--accent);
    }
    .pending-table .ctx {
      color: var(--muted);
      max-width: 280px;
    }
    .tag {
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      background: rgba(37, 211, 102, 0.12);
      color: var(--accent);
    }
    .tag.time { background: rgba(245, 197, 66, 0.12); color: var(--warn); }
    .tag.action { background: rgba(96, 165, 250, 0.12); color: #93c5fd; }
    .tag.catalog { background: rgba(61, 214, 140, 0.15); color: var(--ok); }
    .entities-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem;
    }
    .entities-filters {
      display: grid;
      gap: 0.65rem;
      margin-bottom: 0.85rem;
    }
    @media (min-width: 720px) {
      .entities-filters {
        grid-template-columns: 1.4fr 1fr 1fr 1fr auto;
        align-items: end;
      }
    }
    .filter-field label {
      display: block;
      font-size: 0.72rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.03em;
      margin-bottom: 0.25rem;
    }
    .filter-field input,
    .filter-field select {
      width: 100%;
      background: #0a0e14;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 8px;
      padding: 0.45rem 0.6rem;
      font-size: 0.85rem;
    }
    .filter-check {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.85rem;
      color: var(--muted);
      padding-bottom: 0.35rem;
    }
    .filter-check input { accent-color: var(--accent); }
    .entities-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
      margin-bottom: 0.75rem;
    }
    .entities-count { font-size: 0.85rem; color: var(--muted); margin-left: auto; }
    .entities-table-wrap {
      max-height: 62vh;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    .entities-table { margin: 0; }
    .entities-table tbody tr:hover { background: rgba(255, 255, 255, 0.03); }
    .entities-table tbody tr.selected { background: rgba(37, 211, 102, 0.08); }
    .entities-table td.state-cell { font-family: "Cascadia Code", "Consolas", monospace; font-size: 0.78rem; }
    .entities-table .btn-copy-row {
      padding: 0.2rem 0.5rem;
      font-size: 0.75rem;
    }
    .entities-loading { color: var(--muted); font-size: 0.9rem; padding: 1rem 0; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1><span>Shakira</span></h1>
        <p class="meta" id="meta">Carregando…</p>
      </div>
      <span class="badge-overall loading" id="overall">—</span>
    </header>

    <nav class="tabs">
      <button type="button" class="tab active" data-tab="status">Status</button>
      <button type="button" class="tab" data-tab="entities">Entidades HA</button>
      <button type="button" class="tab" data-tab="yaml-devices">shakira_devices.yaml</button>
      <button type="button" class="tab" data-tab="yaml-cameras">shakira_cameras.yaml</button>
      <button type="button" class="tab" data-tab="yaml-alerts">shakira_alerts.yaml</button>
    </nav>

    <div id="msg-box" class="msg"></div>

    <section id="panel-status" class="panel active">
      <div class="toolbar">
        <button type="button" id="btn-refresh">Atualizar status</button>
        <span class="meta">Atualizacao automatica a cada 30s</span>
      </div>
      <div class="grid" id="grid"></div>
      <h2 class="section-title">Agendamentos pendentes (avisos e acoes)</h2>
      <div class="pending-card" id="scheduled-pending"></div>
    </section>

    <section id="panel-entities" class="panel">
      <div class="entities-card">
        <div class="entities-filters">
          <div class="filter-field">
            <label for="ent-filter-text">Buscar</label>
            <input type="search" id="ent-filter-text" placeholder="entity_id ou nome…" autocomplete="off" />
          </div>
          <div class="filter-field">
            <label for="ent-filter-domain">Tipo (dominio)</label>
            <select id="ent-filter-domain"><option value="">Todos</option></select>
          </div>
          <div class="filter-field">
            <label for="ent-filter-platform">Integracao</label>
            <select id="ent-filter-platform"><option value="">Todas</option></select>
          </div>
          <div class="filter-field">
            <label for="ent-filter-state">Estado</label>
            <input type="text" id="ent-filter-state" placeholder="ex.: on, off, open…" autocomplete="off" />
          </div>
          <label class="filter-check">
            <input type="checkbox" id="ent-filter-catalog" />
            So no catalogo Shakira
          </label>
        </div>
        <div class="entities-toolbar">
          <button type="button" id="btn-entities-refresh">Atualizar do HA</button>
          <button type="button" id="btn-entities-copy" disabled>Copiar selecionados</button>
          <button type="button" id="btn-entities-select-visible">Selecionar visiveis</button>
          <button type="button" id="btn-entities-clear">Limpar selecao</button>
          <span class="entities-count" id="entities-count">Carregando…</span>
        </div>
        <div class="entities-table-wrap" id="entities-table-wrap">
          <p class="entities-loading" id="entities-loading">Carregando entidades…</p>
        </div>
      </div>
    </section>

    <section id="panel-yaml-devices" class="panel">
      <div class="editor-card" data-editor="devices">
        <p class="meta editor-path">Carregando arquivo…</p>
        <p class="meta editor-stats"></p>
        <textarea class="yaml-editor" spellcheck="false" autocomplete="off"></textarea>
        <div class="toolbar" style="margin-top: 0.75rem;">
          <button type="button" class="btn-validate-yaml">Validar</button>
          <button type="button" class="primary btn-save-yaml">Salvar</button>
          <button type="button" class="btn-reload-yaml">Recarregar do disco</button>
        </div>
        <p class="meta">devices: entidades acionaveis · scenarios: instrucoes em prompt para o Gemini</p>
      </div>
    </section>

    <section id="panel-yaml-cameras" class="panel">
      <div class="editor-card" data-editor="cameras">
        <p class="meta editor-path">Carregando arquivo…</p>
        <p class="meta editor-stats"></p>
        <textarea class="yaml-editor" spellcheck="false" autocomplete="off"></textarea>
        <div class="toolbar" style="margin-top: 0.75rem;">
          <button type="button" class="btn-validate-yaml">Validar</button>
          <button type="button" class="primary btn-save-yaml">Salvar</button>
          <button type="button" class="btn-reload-yaml">Recarregar do disco</button>
        </div>
        <p class="meta">cameras: id igual ao Frigate · name, description e group para o assistente</p>
      </div>
    </section>

    <section id="panel-yaml-alerts" class="panel">
      <div class="editor-card" data-editor="alerts">
        <p class="meta editor-path">Carregando arquivo…</p>
        <p class="meta editor-stats"></p>
        <textarea class="yaml-editor" spellcheck="false" autocomplete="off"></textarea>
        <div class="toolbar" style="margin-top: 0.75rem;">
          <button type="button" class="btn-validate-yaml">Validar</button>
          <button type="button" class="primary btn-save-yaml">Salvar</button>
          <button type="button" class="btn-reload-yaml">Recarregar do disco</button>
        </div>
        <p class="meta">Verificacao periodica · aviso via WhatsApp · recovery_* agenda resposta quando voltar ao normal</p>
      </div>
    </section>

    <p class="footer">Assistente WhatsApp · Home Assistant</p>
  </div>
  <script>
    const STATUS_LABELS = { ok: "OK", warning: "Atenção", error: "Erro", disabled: "Desativado" };
    const YAML_EDITORS = {
      devices: {
        getUrl: "api/devices-yaml",
        validateUrl: "api/devices-yaml/validate",
        saveUrl: "api/devices-yaml",
        stats: function(d) {
          return d.devices_count + " dispositivo(s) · " + d.scenarios_count + " cenario(s) · " +
            d.actionable_count + " entidade(s) acionavel(is)";
        }
      },
      cameras: {
        getUrl: "api/cameras-yaml",
        validateUrl: "api/cameras-yaml/validate",
        saveUrl: "api/cameras-yaml",
        stats: function(d) { return d.cameras_count + " camera(s) configurada(s)"; }
      },
      alerts: {
        getUrl: "api/alerts-yaml",
        validateUrl: "api/alerts-yaml/validate",
        saveUrl: "api/alerts-yaml",
        stats: function(d) {
          return d.alerts_count + " alerta(s) · " + d.enabled_count + " ativo(s)";
        }
      }
    };
    const yamlDirty = { devices: false, cameras: false, alerts: false };
    const yamlLoaded = { devices: false, cameras: false, alerts: false };
    let entitiesLoaded = false;
    let entitiesAll = [];
    let entitiesVisible = [];
    const entitiesSelected = new Set();
    let entitiesFilterTimer = null;

    function esc(s) {
      const d = document.createElement("div");
      d.textContent = s == null ? "" : String(s);
      return d.innerHTML;
    }

    function showMsg(text, type) {
      const box = document.getElementById("msg-box");
      box.textContent = text;
      box.className = "msg " + (type || "");
      if (type) setTimeout(function() { box.className = "msg"; }, 8000);
    }

    document.querySelectorAll(".tab").forEach(function(btn) {
      btn.addEventListener("click", function() {
        const tab = btn.getAttribute("data-tab");
        document.querySelectorAll(".tab").forEach(function(b) { b.classList.remove("active"); });
        document.querySelectorAll(".panel").forEach(function(p) { p.classList.remove("active"); });
        btn.classList.add("active");
        document.getElementById("panel-" + tab).classList.add("active");
        if (tab === "yaml-devices" && !yamlLoaded.devices) loadYamlEditor("devices");
        if (tab === "yaml-cameras" && !yamlLoaded.cameras) loadYamlEditor("cameras");
        if (tab === "yaml-alerts" && !yamlLoaded.alerts) loadYamlEditor("alerts");
        if (tab === "entities" && !entitiesLoaded) loadEntities(false);
      });
    });

    function renderCard(svc) {
      const st = svc.status || "disabled";
      let detailsHtml = '';
      
      if (svc.id === "presence_simulator" && svc.details) {
        const hist = svc.details.history || [];
        const histHtml = hist.length > 0 
          ? '<ul style="padding-left: 1.2rem; margin: 0.5rem 0 0; color: var(--muted); font-size: 0.75rem; list-style-type: disc;">' +
            hist.map(function(act) {
              return '<li style="margin-bottom: 0.35rem;"><strong>[' + esc(act.timestamp) + '] ' + esc(act.action) + ':</strong> ' + esc(act.details) + '</li>';
            }).join("") + '</ul>'
          : '<p style="margin: 0.5rem 0 0; color: var(--muted); font-size: 0.75rem;">Nenhuma ação registrada ainda.</p>';
          
        const activeLight = svc.details.active_light ? '<li><strong>Luz Ativa:</strong> <span class="mono" style="color: var(--accent);">' + esc(svc.details.active_light) + '</span></li>' : '<li><strong>Luz Ativa:</strong> nenhuma</li>';
        const controlEntity = '<li><strong>Botão de Controle:</strong> <span class="mono" style="color: var(--accent);">' + esc(svc.details.control_entity) + '</span></li>';
        const nextAction = svc.details.next_action_in_s ? '<li><strong>Próximo acionamento em:</strong> ' + esc(Math.round(svc.details.next_action_in_s)) + 's</li>' : '';
        const activeUntil = svc.details.active_until_in_s ? '<li><strong>Desliga em:</strong> ' + esc(Math.round(svc.details.active_until_in_s)) + 's</li>' : '';
        
        detailsHtml = '<div style="margin-top: 0.5rem; font-size: 0.78rem; border-top: 1px solid var(--border); padding-top: 0.5rem;">' +
          '<ul style="padding-left: 1.2rem; margin: 0; color: var(--text); list-style-type: square;">' +
            controlEntity + activeLight + nextAction + activeUntil +
          '</ul>' +
          '<h3 style="font-size: 0.8rem; margin: 0.75rem 0 0.25rem; font-weight: 600; color: var(--accent);">Últimas 15 ações da simulação:</h3>' +
          histHtml +
          '</div>';
      } else {
        const details = JSON.stringify(svc.details || {}, null, 2);
        detailsHtml = '<pre>' + esc(details) + '</pre>';
      }
      
      return '<article class="card ' + esc(st) + '"><div class="card-head"><h2>' +
        esc(svc.name || svc.id) + '</h2><span class="pill ' + esc(st) + '">' +
        esc(STATUS_LABELS[st] || st) + '</span></div><p class="summary">' +
        esc(svc.summary || "") + '</p><details><summary>Detalhes</summary>' +
        detailsHtml + '</details></article>';
    }

    function formatUptime(sec) {
      if (sec == null) return "";
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      if (h) return h + "h " + m + "m";
      if (m) return m + "m " + s + "s";
      return s + "s";
    }

    function formatDate(iso) {
      if (!iso) return "—";
      try {
        return new Date(iso).toLocaleString("pt-BR");
      } catch (e) {
        return iso;
      }
    }

    function formatPhone(phone) {
      if (!phone) return "—";
      const s = String(phone);
      if (s.length <= 4) return s;
      return "+" + s;
    }

    function renderScheduledPending(items) {
      const box = document.getElementById("scheduled-pending");
      if (!items || !items.length) {
        box.innerHTML = '<p class="pending-empty">Nenhum agendamento pendente.</p>';
        return;
      }
      const rows = items.map(function(item) {
        const isAction = item.kind === "action";
        const tagClass = isAction ? "tag action" : (item.trigger_type === "time" ? "tag time" : "tag");
        const typeLabel = isAction ? "Acao" : "Aviso";
        const triggerLabel = item.trigger_type === "time" ? "Tempo" : "Entidade";
        const label = item.label ? esc(item.label) : '<span class="meta">—</span>';
        const actionHint = isAction && item.action_entity_id
          ? '<br><span class="mono">' + esc(item.action_domain + "/" + item.action_service) +
            " @ " + esc(item.action_entity_id) + '</span>'
          : "";
        return '<tr>' +
          '<td class="mono">' + esc(item.id) + '</td>' +
          '<td><span class="' + tagClass + '">' + esc(typeLabel) + '</span></td>' +
          '<td>' + esc(formatPhone(item.phone)) + '</td>' +
          '<td>' + label + '</td>' +
          '<td><span class="tag">' + esc(triggerLabel) + '</span><br>' +
            esc(item.trigger_summary || "") + actionHint + '</td>' +
          '<td class="ctx">' + esc(item.context || "") + '</td>' +
          '<td>' + esc(formatDate(item.created_at)) + '</td>' +
          '<td>' + esc(formatDate(item.expires_at)) + '</td>' +
          '<td><button type="button" class="btn-cancel-schedule" data-phone="' + esc(item.phone) + '" data-id="' + esc(item.id) + '" style="background: rgba(248, 113, 113, 0.15); border-color: var(--err); color: var(--err); padding: 0.2rem 0.55rem; font-size: 0.72rem; border-radius: 6px; cursor: pointer; font-weight: 600;">Excluir</button></td>' +
          '</tr>';
      }).join("");
      box.innerHTML = '<table class="pending-table"><thead><tr>' +
        '<th>ID</th><th>Tipo</th><th>Telefone</th><th>Label</th><th>Trigger</th>' +
        '<th>Contexto</th><th>Criado</th><th>Expira</th><th>Ações</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>';
        
      box.querySelectorAll(".btn-cancel-schedule").forEach(function(btn) {
        btn.addEventListener("click", function() {
          const phone = btn.getAttribute("data-phone");
          const id = btn.getAttribute("data-id");
          if (phone && id && confirm("Deseja realmente cancelar este agendamento?")) {
            cancelSchedule(phone, id);
          }
        });
      });
    }

    async function cancelSchedule(phone, id) {
      try {
        const r = await fetch("api/scheduled-responses/" + phone + "/" + id, {
          method: "DELETE"
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "Erro HTTP " + r.status);
        showMsg(data.message || "Agendamento cancelado com sucesso.", "ok");
        loadStatus();
      } catch (e) {
        showMsg("Não foi possível excluir: " + e.message, "error");
      }
    }

    async function loadStatus() {
      try {
        const r = await fetch("api/status", { headers: { Accept: "application/json" } });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        document.getElementById("overall").textContent = STATUS_LABELS[data.overall] || data.overall;
        document.getElementById("overall").className = "badge-overall " + (data.overall || "loading");
        const gen = data.generated_at ? new Date(data.generated_at).toLocaleString("pt-BR") : "—";
        const up = data.uptime_seconds != null ? " · Uptime " + formatUptime(data.uptime_seconds) : "";
        document.getElementById("meta").textContent = "v" + (data.version || "?") + " · " + gen + up;
        document.getElementById("grid").innerHTML = (data.services || []).map(renderCard).join("");
        renderScheduledPending(data.scheduled_pending || []);
      } catch (e) {
        showMsg("Nao foi possivel carregar o status: " + e.message, "error");
      }
    }

    function editorCard(kind) {
      return document.querySelector('.editor-card[data-editor="' + kind + '"]');
    }

    async function loadYamlEditor(kind) {
      const cfg = YAML_EDITORS[kind];
      const card = editorCard(kind);
      if (!cfg || !card) return;
      try {
        const r = await fetch(cfg.getUrl);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        card.querySelector(".yaml-editor").value = data.content || "";
        yamlDirty[kind] = false;
        yamlLoaded[kind] = true;
        const exists = data.exists ? "existente" : "novo (ainda nao gravado no disco)";
        card.querySelector(".editor-path").textContent = data.path + " · " + exists;
        card.querySelector(".editor-stats").textContent = cfg.stats(data);
      } catch (e) {
        showMsg("Erro ao carregar YAML: " + e.message, "error");
      }
    }

    function formatApiErrors(data) {
      const d = data && data.detail;
      if (!d) return "HTTP erro";
      if (typeof d === "string") return d;
      if (d.errors && Array.isArray(d.errors)) return d.errors.join("\\n");
      if (Array.isArray(d)) return d.map(function(x) { return x.msg || x; }).join("\\n");
      return JSON.stringify(d);
    }

    async function validateYamlEditor(kind) {
      const cfg = YAML_EDITORS[kind];
      const card = editorCard(kind);
      const content = card.querySelector(".yaml-editor").value;
      try {
        const r = await fetch(cfg.validateUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: content })
        });
        const data = await r.json();
        if (!r.ok) throw new Error(formatApiErrors(data));
        if (data.valid) {
          showMsg("Estrutura valida. Pode salvar.", "ok");
        } else {
          showMsg("Estrutura invalida:\\n" + (data.errors || []).join("\\n"), "error");
        }
      } catch (e) {
        showMsg("Validacao falhou: " + e.message, "error");
      }
    }

    async function saveYamlEditor(kind) {
      const cfg = YAML_EDITORS[kind];
      const card = editorCard(kind);
      const content = card.querySelector(".yaml-editor").value;
      try {
        const r = await fetch(cfg.saveUrl, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: content })
        });
        const data = await r.json();
        if (!r.ok) throw new Error(formatApiErrors(data));
        yamlDirty[kind] = false;
        card.querySelector(".editor-stats").textContent = cfg.stats(data);
        showMsg(data.message || "Salvo com sucesso.", "ok");
        loadStatus();
      } catch (e) {
        showMsg("Erro ao salvar: " + e.message, "error");
      }
    }

    document.querySelectorAll(".editor-card").forEach(function(card) {
      const kind = card.getAttribute("data-editor");
      card.querySelector(".yaml-editor").addEventListener("input", function() {
        yamlDirty[kind] = true;
      });
      card.querySelector(".btn-validate-yaml").addEventListener("click", function() {
        validateYamlEditor(kind);
      });
      card.querySelector(".btn-save-yaml").addEventListener("click", function() {
        saveYamlEditor(kind);
      });
      card.querySelector(".btn-reload-yaml").addEventListener("click", function() {
        if (yamlDirty[kind] && !confirm("Descartar alteracoes nao salvas?")) return;
        yamlLoaded[kind] = false;
        loadYamlEditor(kind);
      });
    });
    document.getElementById("btn-refresh").addEventListener("click", loadStatus);

    function fillEntitySelect(selectId, values, emptyLabel) {
      const sel = document.getElementById(selectId);
      const current = sel.value;
      sel.innerHTML = '<option value="">' + esc(emptyLabel) + '</option>' +
        values.map(function(v) {
          return '<option value="' + esc(v) + '">' + esc(v) + '</option>';
        }).join("");
      if (values.indexOf(current) >= 0) sel.value = current;
    }

    function entityFilterValues() {
      return {
        text: (document.getElementById("ent-filter-text").value || "").trim().toLowerCase(),
        domain: document.getElementById("ent-filter-domain").value || "",
        platform: document.getElementById("ent-filter-platform").value || "",
        state: (document.getElementById("ent-filter-state").value || "").trim().toLowerCase(),
        catalogOnly: document.getElementById("ent-filter-catalog").checked
      };
    }

    function filterEntities(list) {
      const f = entityFilterValues();
      return list.filter(function(ent) {
        if (f.domain && ent.domain !== f.domain) return false;
        if (f.platform && ent.platform !== f.platform) return false;
        if (f.catalogOnly && !ent.in_catalog) return false;
        if (f.state && String(ent.state || "").toLowerCase().indexOf(f.state) < 0) return false;
        if (f.text) {
          const hay = (ent.entity_id + " " + (ent.friendly_name || "")).toLowerCase();
          if (hay.indexOf(f.text) < 0) return false;
        }
        return true;
      });
    }

    function updateEntitiesCount() {
      const el = document.getElementById("entities-count");
      const sel = entitiesSelected.size;
      el.textContent = entitiesVisible.length + " visiveis de " + entitiesAll.length +
        (sel ? " · " + sel + " selecionada(s)" : "");
      document.getElementById("btn-entities-copy").disabled = sel === 0;
    }

    function renderEntitiesTable() {
      const wrap = document.getElementById("entities-table-wrap");
      entitiesVisible = filterEntities(entitiesAll);
      updateEntitiesCount();

      if (!entitiesVisible.length) {
        wrap.innerHTML = '<p class="entities-loading">Nenhuma entidade corresponde aos filtros.</p>';
        return;
      }

      const allVisibleSelected = entitiesVisible.length > 0 &&
        entitiesVisible.every(function(ent) { return entitiesSelected.has(ent.entity_id); });

      const rows = entitiesVisible.map(function(ent) {
        const checked = entitiesSelected.has(ent.entity_id) ? " checked" : "";
        const rowClass = checked ? " selected" : "";
        const catalogBadge = ent.in_catalog
          ? ' <span class="tag catalog">catalogo</span>' : "";
        const platform = ent.platform ? esc(ent.platform) : '<span class="meta">—</span>';
        const deviceClass = ent.device_class ? esc(ent.device_class) : '<span class="meta">—</span>';
        const name = ent.friendly_name ? esc(ent.friendly_name) : '<span class="meta">—</span>';
        return '<tr class="' + rowClass.trim() + '" data-eid="' + esc(ent.entity_id) + '">' +
          '<td><input type="checkbox" class="ent-row-check"' + checked + ' aria-label="Selecionar"></td>' +
          '<td class="mono">' + esc(ent.entity_id) + catalogBadge + '</td>' +
          '<td>' + name + '</td>' +
          '<td>' + esc(ent.domain) + '</td>' +
          '<td>' + platform + '</td>' +
          '<td>' + deviceClass + '</td>' +
          '<td class="state-cell">' + esc(ent.state) + '</td>' +
          '<td><button type="button" class="btn-copy-row" data-eid="' + esc(ent.entity_id) + '">Copiar</button></td>' +
          '</tr>';
      }).join("");

      wrap.innerHTML = '<table class="pending-table entities-table"><thead><tr>' +
        '<th><input type="checkbox" id="ent-check-all"' + (allVisibleSelected ? " checked" : "") + ' aria-label="Selecionar visiveis"></th>' +
        '<th>Entity ID</th><th>Nome</th><th>Tipo</th><th>Integracao</th><th>Device class</th><th>Estado</th><th></th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>';

      document.getElementById("ent-check-all").addEventListener("change", function(ev) {
        if (ev.target.checked) {
          entitiesVisible.forEach(function(ent) { entitiesSelected.add(ent.entity_id); });
        } else {
          entitiesVisible.forEach(function(ent) { entitiesSelected.delete(ent.entity_id); });
        }
        renderEntitiesTable();
      });

      wrap.querySelectorAll(".ent-row-check").forEach(function(cb) {
        cb.addEventListener("change", function(ev) {
          const tr = ev.target.closest("tr");
          const eid = tr && tr.getAttribute("data-eid");
          if (!eid) return;
          if (ev.target.checked) entitiesSelected.add(eid);
          else entitiesSelected.delete(eid);
          renderEntitiesTable();
        });
      });

      wrap.querySelectorAll(".btn-copy-row").forEach(function(btn) {
        btn.addEventListener("click", function() {
          const eid = btn.getAttribute("data-eid");
          if (eid) copyEntityIds([eid]);
        });
      });
    }

    async function copyEntityIds(ids) {
      const text = ids.join("\\n");
      try {
        await navigator.clipboard.writeText(text);
        showMsg(ids.length === 1
          ? "Copiado: " + ids[0]
          : ids.length + " entity_id(s) copiados.", "ok");
      } catch (e) {
        showMsg("Nao foi possivel copiar: " + e.message, "error");
      }
    }

    function scheduleEntitiesFilter() {
      if (entitiesFilterTimer) clearTimeout(entitiesFilterTimer);
      entitiesFilterTimer = setTimeout(renderEntitiesTable, 200);
    }

    async function loadEntities(refresh) {
      const wrap = document.getElementById("entities-table-wrap");
      wrap.innerHTML = '<p class="entities-loading">Carregando entidades…</p>';
      document.getElementById("entities-count").textContent = "Carregando…";
      try {
        const url = "api/entities" + (refresh ? "?refresh=true" : "");
        const r = await fetch(url, { headers: { Accept: "application/json" } });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        entitiesAll = data.entities || [];
        entitiesLoaded = true;
        fillEntitySelect("ent-filter-domain", data.domains || [], "Todos");
        fillEntitySelect("ent-filter-platform", data.platforms || [], "Todas");
        renderEntitiesTable();
        if (refresh) showMsg("Lista atualizada do Home Assistant.", "ok");
      } catch (e) {
        wrap.innerHTML = '<p class="entities-loading">Erro ao carregar entidades.</p>';
        showMsg("Nao foi possivel carregar entidades: " + e.message, "error");
      }
    }

    ["ent-filter-text", "ent-filter-state"].forEach(function(id) {
      document.getElementById(id).addEventListener("input", scheduleEntitiesFilter);
    });
    ["ent-filter-domain", "ent-filter-platform", "ent-filter-catalog"].forEach(function(id) {
      document.getElementById(id).addEventListener("change", renderEntitiesTable);
    });
    document.getElementById("btn-entities-refresh").addEventListener("click", function() {
      loadEntities(true);
    });
    document.getElementById("btn-entities-copy").addEventListener("click", function() {
      if (!entitiesSelected.size) return;
      copyEntityIds(Array.from(entitiesSelected).sort());
    });
    document.getElementById("btn-entities-select-visible").addEventListener("click", function() {
      entitiesVisible.forEach(function(ent) { entitiesSelected.add(ent.entity_id); });
      renderEntitiesTable();
    });
    document.getElementById("btn-entities-clear").addEventListener("click", function() {
      entitiesSelected.clear();
      renderEntitiesTable();
    });

    loadStatus();
    setInterval(loadStatus, 30000);
  </script>
</body>
</html>
"""


def get_dashboard_html() -> str:
    return DASHBOARD_HTML
