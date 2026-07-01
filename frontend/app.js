/* KIRA — Redesigned Pipeline UI
 *
 * Flow Architecture:
 * ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌─────────┐
 * │ 1.Alert │→│2.Evidence │→│3.War Room│→│4.Decision│→│5.Remediate│→│ 6.Close │
 * └─────────┘  └──────────┘  └──────────┘  └──────────┘  └───────────┘  └─────────┘
 *
 * - Jira ticket card appears after alert
 * - Evidence steps are grouped into a collapsible card
 * - War Room shows all 4 agents in a grid (not sequential)
 * - Closing summary shows Jira link, time, outcome
 */

const API = '';
let activeIncidentId   = null;
let activeSSE          = null;
let incidentIsTerminal = false;
const TERMINAL_STATUSES = new Set(['resolved', 'failed', 'skipped']);

// Pipeline stage tracking
const STAGES = ['alert', 'evidence', 'warroom', 'decision', 'remediation', 'close'];
let currentStage = '';

// Collected data for grouped rendering
let evidenceItems = [];
let warRoomAgents = {};  // step name → {status, detail, timestamp}
let warRoomComplete = false;
let jiraKey = null;
let jiraUrl = null;
let incidentMeta = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const elList       = $('incident-list');
const elCount      = $('incident-count');
const elTimeline   = $('detail-content');
const elEmpty      = $('timeline-empty');
const elHeader     = $('detail-header');
const elTitle      = $('detail-title');
const elNs         = $('detail-ns');
const elId         = $('detail-id');
const elStarted    = $('detail-started');
const elBadge      = $('detail-badge');
const elApprovalBanner = $('approval-banner');
const elApprovalBody   = $('approval-body');
const elBtnApprove     = $('btn-approve');
const elBtnSkip        = $('btn-skip');
const elResBanner      = $('resolved-banner');
const elResIcon    = $('banner-icon');
const elResTitle   = $('banner-title');
const elResSub     = $('banner-sub');
const elHealthDot  = $('health-dot');
const elHealthLabel= $('health-label');
const elTriggerBtn = $('trigger-btn');
const elRefreshBtn = $('refresh-btn');

// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function fmtElapsed(s) {
  if (s == null) return '';
  return s < 60 ? `${Math.round(s)}s` : `${Math.floor(s/60)}m ${Math.round(s%60)}s`;
}
function badgeHtml(status) {
  const icons = {pending:'⏳',investigating:'🔍',rca_complete:'🧠',awaiting_approval:'⚠️',
    remediating:'🔧',validating:'✅',resolved:'✓',failed:'✗',skipped:'⏭'};
  return `<span class="badge badge-${status}">${icons[status]||'?'} ${status.replace(/_/g,' ')}</span>`;
}

// ── Pipeline Progress Bar ──────────────────────────────────────────────────
function createPipelineBar() {
  const existing = elTimeline.querySelector('.pipeline-bar');
  if (existing) return existing;

  const bar = document.createElement('div');
  bar.className = 'pipeline-bar';
  bar.innerHTML = STAGES.map((s, i) => {
    const labels = {alert:'Alert',evidence:'Evidence',warroom:'War Room',
      decision:'Decision',remediation:'Remediation',close:'Resolution'};
    const icons = {alert:'🚨',evidence:'🔍',warroom:'🤖',decision:'🧠',
      remediation:'⚡',close:'✓'};
    return `
      <div class="pipe-stage" id="pipe-${s}" data-stage="${s}">
        <div class="pipe-icon">${icons[s]}</div>
        <div class="pipe-label">${labels[s]}</div>
      </div>
      ${i < STAGES.length-1 ? '<div class="pipe-connector"></div>' : ''}`;
  }).join('');

  elTimeline.insertBefore(bar, elTimeline.firstChild);
  return bar;
}

function updatePipeline(stage) {
  if (!stage || stage === currentStage) return;
  currentStage = stage;
  const idx = STAGES.indexOf(stage);
  STAGES.forEach((s, i) => {
    const el = $(`pipe-${s}`);
    if (!el) return;
    el.classList.remove('active','completed','current');
    if (i < idx) el.classList.add('completed');
    else if (i === idx) el.classList.add('active','current');
  });
  // Animate connectors
  document.querySelectorAll('.pipe-connector').forEach((c, i) => {
    c.classList.toggle('filled', i < idx);
  });
}

// ── Stage detection from SSE step names ────────────────────────────────────
function detectStage(step) {
  const s = step.toLowerCase();
  if (s === 'jira' || s === 'investigation started' || s === 'alert received') return 'alert';
  if (['pod status','pod logs','resource usage','rollout history',
       'deployment describe','recent events','correlated services'].includes(s)) return 'evidence';
  if (s.startsWith('war room')) return 'warroom';
  if (s === 'decision engine' || s === 'auto-approved') return 'decision';
  if (s === 'remediation' || s === 'awaiting approval' || s === 'retry') return 'remediation';
  if (s === 'validation' || s.includes('incident closed') || s === 'pipeline error') return 'close';
  return '';
}

function isEvidenceStep(step) {
  return ['pod status','pod logs','resource usage','rollout history',
    'deployment describe','recent events','correlated services'].includes(step.toLowerCase());
}

function isWarRoomAgent(step) {
  const s = step.toLowerCase();
  return s.startsWith('war room') && (s.includes('sre') || s.includes('app') ||
    s.includes('security') || s.includes('cost'));
}

// ── Health check ───────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const r = await fetch(`${API}/health`);
    const d = await r.json();
    const ok = d.llm === 'reachable';
    elHealthDot.className = `health-dot ${ok ? 'ok' : 'error'}`;
    const model = d.model || 'Ollama';
    const ctx   = d.cluster_context ? ` · ${d.cluster_context}` : '';
    elHealthLabel.textContent = ok
      ? `${model} ✓  |  ${d.active_incidents} active${ctx}`
      : 'LLM unreachable';
  } catch {
    elHealthDot.className = 'health-dot error';
    elHealthLabel.textContent = 'Backend unreachable';
  }
}

// ── Incident list ──────────────────────────────────────────────────────────
async function loadIncidents() {
  try {
    const r = await fetch(`${API}/incidents`);
    if (!r.ok) return;
    const list = await r.json();
    elCount.textContent = list.length;

    // ── Metrics ───────────────────────────────────────────────────────────
    const TERMINAL = new Set(['resolved', 'failed', 'skipped']);
    const active   = list.filter(i => !TERMINAL.has(i.status)).length;
    const resolved = list.filter(i => i.status === 'resolved').length;
    const failed   = list.filter(i => i.status === 'failed').length;
    const total    = list.length;

    const resolvedWithTime = list.filter(
      i => i.status === 'resolved' && i.total_time_seconds != null
    );
    const mttr = resolvedWithTime.length
      ? resolvedWithTime.reduce((s, i) => s + i.total_time_seconds, 0) / resolvedWithTime.length
      : null;

    const withConf = list.filter(i => i.confidence != null);
    const avgConf  = withConf.length
      ? Math.round(withConf.reduce((s, i) => s + i.confidence, 0) / withConf.length)
      : null;

    const autoResolved = list.filter(
      i => i.status === 'resolved' && i.source !== 'manual'
    ).length;

    const setMetric = (id, val) => {
      const el = document.getElementById(id);
      if (!el || el.textContent === String(val)) return;
      el.textContent = val;
      el.classList.remove('pulse');
      void el.offsetWidth;
      el.classList.add('pulse');
    };

    setMetric('m-active',     active);
    setMetric('m-resolved',   resolved);
    setMetric('m-failed',     failed);
    setMetric('m-total',      total);
    setMetric('m-mttr',       mttr != null ? fmtElapsed(mttr) : '—');
    setMetric('m-confidence', avgConf != null ? `${avgConf}%` : '—');
    setMetric('m-auto',       autoResolved);
    // ─────────────────────────────────────────────────────────────────────

    if (!list.length) {
      elList.innerHTML = `<div class="timeline-empty" style="padding:32px 0">
        <div class="timeline-empty-icon">📭</div>
        <div>No incidents yet.<br/>Trigger one to get started.</div></div>`;
      return;
    }

    elList.innerHTML = '';

    const openIncidents = list.filter(inc => !TERMINAL_STATUSES.has(inc.status));
    const resolvedIncidents = list.filter(inc => TERMINAL_STATUSES.has(inc.status));

    function renderCard(inc) {
      return `
      <div class="incident-card ${inc.id===activeIncidentId?'active':''}"
           data-status="${inc.status}" data-id="${inc.id}"
           onclick="selectIncident('${inc.id}')">
        <div class="card-header">
          <span class="card-service">${esc(inc.service)}</span>
          ${badgeHtml(inc.status)}
        </div>
        <div class="card-ns">ns: ${esc(inc.namespace)}</div>
        <div class="card-msg">${esc(inc.message)}</div>
        <div class="card-footer">
          <span class="card-time">${fmtTime(inc.started_at)}</span>
          ${inc.total_time_seconds!=null ? `<span class="card-time">⏱ ${fmtElapsed(inc.total_time_seconds)}</span>` : ''}
          ${inc.source ? `<span class="card-source">📡 ${esc(inc.source)}</span>` : ''}
          ${inc.jira_key ? `<span class="card-jira">🎫 ${esc(inc.jira_key)}</span>` : ''}
        </div>
      </div>`;
    }

    // Open group (always show)
    elList.innerHTML += `
      <div class="incident-group">
        <div class="incident-group-header open" onclick="this.parentElement.classList.toggle('collapsed')">
          <span class="incident-group-arrow">▼</span>
          <span class="incident-group-title">Open</span>
          <span class="incident-group-count">${openIncidents.length}</span>
        </div>
        <div class="incident-group-body">
          ${openIncidents.length ? openIncidents.map(renderCard).join('') : '<div style="padding:12px;color:var(--muted);font-size:12px;text-align:center;">No open incidents</div>'}
        </div>
      </div>`;

    // Resolved group (collapsed by default, always show)
    elList.innerHTML += `
      <div class="incident-group collapsed">
        <div class="incident-group-header resolved" onclick="this.parentElement.classList.toggle('collapsed')">
          <span class="incident-group-arrow">▼</span>
          <span class="incident-group-title">Resolved</span>
          <span class="incident-group-count">${resolvedIncidents.length}</span>
        </div>
        <div class="incident-group-body">
          ${resolvedIncidents.length ? resolvedIncidents.map(renderCard).join('') : '<div style="padding:12px;color:var(--muted);font-size:12px;text-align:center;">No resolved incidents</div>'}
        </div>
      </div>`;

    if (activeIncidentId) {
      const a = list.find(i => i.id === activeIncidentId);
      if (a) elBadge.innerHTML = badgeHtml(a.status);
    }
  } catch(e) { console.error('loadIncidents:', e); }
}

// ── Select incident ────────────────────────────────────────────────────────
async function selectIncident(id) {
  if (id === activeIncidentId) return;
  if (activeSSE) { activeSSE.close(); activeSSE = null; }

  activeIncidentId = id;
  incidentIsTerminal = false;
  currentStage = '';
  evidenceItems = [];
  warRoomAgents = {};
  warRoomComplete = false;
  jiraKey = null;
  jiraUrl = null;
  incidentMeta = null;

  // Reset UI
  elTimeline.innerHTML = '';
  elTimeline.appendChild(elEmpty);
  elEmpty.style.display = '';
  elApprovalBanner.classList.remove('visible');
  elResBanner.classList.remove('visible','resolved','failed');
  elHeader.style.display = 'block';
  const chatFab = $('chat-fab');
  if (chatFab) chatFab.style.display = 'none';
  const chatMsgs = $('chat-messages');
  if (chatMsgs) { chatMsgs.innerHTML = ''; chatMsgs.appendChild(buildChatWelcome()); }
  closeChat();
  $('btn-postmortem').style.display = 'none';
  $('pm-overlay').style.display = 'none';

  try {
    const r = await fetch(`${API}/incidents/${id}`);
    const inc = await r.json();
    incidentMeta = inc;
    elTitle.textContent = `${inc.alert.service} — Incident ${inc.id}`;
    elNs.textContent = inc.alert.namespace;
    elId.textContent = `#${inc.id}`;
    elStarted.textContent = fmtTime(inc.started_at);
    elBadge.innerHTML = badgeHtml(inc.status);

    if (TERMINAL_STATUSES.has(inc.status)) {
      incidentIsTerminal = true;
      stopReloadAnimation();
    }
    if (inc.status === 'awaiting_approval') {
      showApprovalBanner(
        inc.rca ? inc.rca.remediation_type : null,
        inc.rca ? inc.rca.confidence : null,
        inc.rca ? inc.rca.remediation_command : null
      );
    }
    if (chatFab) chatFab.style.display = 'flex';
  } catch(e) { console.error('selectIncident:', e); }

  document.querySelectorAll('.incident-card').forEach(c =>
    c.classList.toggle('active', c.dataset.id === id));

  startSSE(id);
}

function deselectIncident() {
  if (activeSSE) { activeSSE.close(); activeSSE = null; }
  activeIncidentId = null;
  currentStage = '';
  evidenceItems = [];
  warRoomAgents = {};
  stopReloadAnimation();

  elHeader.style.display = 'none';
  elApprovalBanner.classList.remove('visible');
  elResBanner.classList.remove('visible','resolved','failed');
  elTimeline.innerHTML = '';
  elTimeline.appendChild(elEmpty);
  elEmpty.style.display = '';

  const chatFab = $('chat-fab');
  if (chatFab) chatFab.style.display = 'none';
  closeChat();
  $('btn-postmortem').style.display = 'none';
  $('pm-overlay').style.display = 'none';
  document.querySelectorAll('.incident-card').forEach(c => c.classList.remove('active'));
}

// ── SSE stream ─────────────────────────────────────────────────────────────
function startSSE(id) {
  const es = new EventSource(`${API}/incidents/${id}/stream`);
  activeSSE = es;
  startReloadAnimation();

  es.onmessage = async (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'done') {
      incidentIsTerminal = true;
      stopReloadAnimation();
      updatePipeline('close');
      renderClosingSummary(data.status, data.total_time_seconds);
      es.close();
      loadIncidents();
      return;
    }
    if (data.type === 'heartbeat') return;

    handleTimelineEvent(data);
  };

  es.onerror = () => console.debug('SSE reconnecting…');
}

// ── Main event router ──────────────────────────────────────────────────────
async function handleTimelineEvent(data) {
  elEmpty.style.display = 'none';
  createPipelineBar();

  const stage = detectStage(data.step);
  if (stage) updatePipeline(stage);

  const step = data.step.toLowerCase();

  // ── Jira ticket notification
  if (step === 'jira' && data.status === 'info') {
    const match = data.detail.match(/Ticket created:\s*(\S+)/);
    if (match) {
      jiraKey = match[1];
      // Fetch jira_url from incidents API
      try {
        const r = await fetch(`${API}/incidents/${activeIncidentId}`);
        const inc = await r.json();
        jiraUrl = inc.jira_url || `https://your-org.atlassian.net/browse/${jiraKey}`;
      } catch { jiraUrl = null; }
      renderJiraCard();
    }
    return;
  }

  // ── Alert / Investigation Started
  if (step === 'investigation started' || step === 'alert received') {
    renderAlertCard(data);
    return;
  }

  // ── Evidence steps → collect and group
  if (isEvidenceStep(data.step)) {
    evidenceItems.push(data);
    renderEvidenceCard();
    return;
  }

  // ── War Room agents → collect into grid
  if (isWarRoomAgent(data.step)) {
    warRoomAgents[data.step] = data;
    renderWarRoomCard();
    return;
  }

  // ── War Room header/footer (not individual agents)
  if (step === 'war room' && data.detail && data.detail.toLowerCase().includes('all')) {
    warRoomComplete = true;
    renderWarRoomCard();
    return;
  }
  if (step === 'war room') return; // Skip "launched" message

  // ── Decision Engine
  if (step === 'decision engine') {
    if (data.status === 'success') {
      renderDecisionCard(data);
      // Fetch full RCA + war room opinions
      try {
        const r = await fetch(`${API}/incidents/${activeIncidentId}`);
        const inc = await r.json();
        if (inc.rca) renderRCADetail(inc.rca);
        if (inc.agent_opinions && inc.agent_opinions.length) renderFullWarRoom(inc.agent_opinions);
      } catch { /* non-fatal */ }
    } else if (data.status === 'running') {
      renderDecisionRunning();
    }
    return;
  }

  // ── Auto-Approved
  if (step === 'auto-approved') {
    renderInfoCard('auto-approved', '⚡', 'Auto-Approved', data.detail);
    return;
  }

  // ── Remediation
  if (step === 'remediation') {
    renderRemediationCard(data);
    return;
  }

  // ── Validation
  if (step === 'validation') {
    renderInfoCard('validation', data.status === 'success' ? '✅' : '🔄',
      'Validation', data.detail);
    return;
  }

  // ── Awaiting Approval
  if (step === 'awaiting approval' && !incidentIsTerminal) {
    const actMatch = data.detail.match(/(?:Recommended:|before executing:)\s*(?:RemediationType\.)?(\w+)/i);
    const confMatch = data.detail.match(/confidence\s+(\d+)%/i);
    const cmdMatch = data.detail.match(/Command:\s*(.+?)(?:\s*\|\s*|$)/);
    showApprovalBanner(
      actMatch ? actMatch[1].toLowerCase() : null,
      confMatch ? parseInt(confMatch[1]) : null,
      cmdMatch ? cmdMatch[1].trim() : null
    );
    return;
  }

  // ── Incident Closed
  if (step.includes('incident closed')) {
    updatePipeline('close');
    return; // The 'done' SSE event handles the actual closing
  }

  // ── Retry → reset collectors for new cycle
  if (step === 'retry') {
    evidenceItems = [];
    warRoomAgents = {};
    warRoomComplete = false;
    renderInfoCard('retry', '🔄', 'Retry', data.detail);
    return;
  }

  // ── Fallback: generic timeline item
  renderGenericItem(data);
}

// ── Render: Alert card ─────────────────────────────────────────────────────
function renderAlertCard(data) {
  const sourceMatch = data.detail && data.detail.match(/triggered from (.+)/i);
  const source = sourceMatch ? sourceMatch[1] : null;

  // Toast + radar flash for all incidents
  const svc = incidentMeta?.alert?.service || 'Service';
  showAlertToast(svc);
  const radar = document.getElementById('scanning-radar');
  if (radar) { radar.classList.add('threat'); setTimeout(() => radar.classList.remove('threat'), 8000); }

  upsertCard('card-alert', 'flow-card flow-alert', `
    <div class="flow-card-header">
      <span class="flow-icon alert-pulse">🚨</span>
      <span class="flow-title">Alert Received</span>
      ${source ? `<span class="flow-source">📡 ${esc(source)}</span>` : ''}
      <span class="flow-time">${fmtTime(data.timestamp)}</span>
    </div>
    <div class="flow-card-body">
      <div class="flow-detail">${esc(data.detail)}</div>
    </div>`);
}

// ── Render: Jira card ──────────────────────────────────────────────────────
function renderJiraCard() {
  if (!jiraKey) return;
  const link = jiraUrl
    ? `<a href="${esc(jiraUrl)}" target="_blank" class="jira-link">${esc(jiraKey)} ↗</a>`
    : `<span class="jira-key">${esc(jiraKey)}</span>`;
  upsertCard('card-jira', 'flow-card flow-jira', `
    <div class="flow-card-header">
      <span class="flow-icon">🎫</span>
      <span class="flow-title">Jira Ticket Created</span>
    </div>
    <div class="flow-card-body jira-body">
      <div class="jira-ticket-badge">${link}</div>
      <div class="jira-status-label">Tracking incident in Jira</div>
    </div>`);
}

// ── Render: Evidence card (grouped) ────────────────────────────────────────
function renderEvidenceCard() {
  const total = 7;
  // Deduplicate by step name (retries re-add same steps)
  const seen = new Map();
  evidenceItems.forEach(e => seen.set(e.step, e));
  const unique = Array.from(seen.values());
  const done = unique.filter(e => e.status !== 'running').length;
  const running = unique.some(e => e.status === 'running');
  const pct = Math.min(100, Math.round((done / total) * 100));

  const itemsHtml = unique.map(ev => {
    const isDone = ev.status === 'success';
    const isErr = ev.status === 'error';
    const isRun = ev.status === 'running';
    const icon = isDone ? '✓' : isErr ? '✗' : isRun ? '◌' : '·';
    const cls = isDone ? 'ev-done' : isErr ? 'ev-error' : isRun ? 'ev-running' : '';
    return `
      <div class="ev-item ${cls}">
        <span class="ev-icon">${icon}</span>
        <span class="ev-name">${esc(ev.step)}</span>
      </div>`;
  }).join('');

  const headerIcon = running ? '🔍' : '✅';

  upsertCard('card-evidence', 'flow-card flow-evidence', `
    <div class="flow-card-header">
      <span class="flow-icon">${headerIcon}</span>
      <span class="flow-title">Evidence Collection</span>
      <span class="flow-count">${done}/${total}</span>
    </div>
    <div class="ev-progress-bar"><div class="ev-progress-fill" style="width:${pct}%"></div></div>
    <div class="flow-card-body ev-grid">${itemsHtml}</div>`);
}

// ── Render: War Room card (unified grid) ───────────────────────────────────
function renderWarRoomCard() {
  const agents = [
    { key: 'sre', icon: '🔧', name: 'SRE Agent', color: '#818cf8' },
    { key: 'app', icon: '📱', name: 'App Agent', color: '#34d399' },
    { key: 'security', icon: '🔒', name: 'Security Agent', color: '#f472b6' },
    { key: 'cost', icon: '💰', name: 'Cost Agent', color: '#fbbf24' },
  ];

  const agentsHtml = agents.map(a => {
    const matchKey = Object.keys(warRoomAgents).find(k => k.toLowerCase().includes(a.key));
    const data = matchKey ? warRoomAgents[matchKey] : null;
    const status = data ? data.status : 'pending';
    const cls = status === 'success' ? 'wr-done' : status === 'running' ? 'wr-running' : 'wr-pending';
    return `
      <div class="wr-agent ${cls}" style="--agent-color:${a.color}">
        <div class="wr-agent-header">
          <span class="wr-agent-icon">${a.icon}</span>
          <span class="wr-agent-name">${a.name}</span>
          <span class="wr-agent-status">${
            status === 'success' ? '✓' : status === 'running' ? '<span class="wr-spinner"></span>' : '⏳'
          }</span>
        </div>
      </div>`;
  }).join('');

  const doneCount = Object.values(warRoomAgents).filter(d => d.status !== 'running').length;

  upsertCard('card-warroom', 'flow-card flow-warroom', `
    <div class="flow-card-header">
      <span class="flow-icon">🤖</span>
      <span class="flow-title">Multi-Agent War Room</span>
      <span class="flow-count">${doneCount}/4 agents</span>
    </div>
    <div class="flow-card-body wr-grid">${agentsHtml}</div>
    ${warRoomComplete ? '<div class="wr-footer">All agents reported → forwarding to Decision Engine</div>' : ''}`);
}

// ── Render: Full War Room (expanded, from API) ─────────────────────────────
function renderFullWarRoom(opinions) {
  const agentsHtml = opinions.map(op => {
    const pct = op.confidence ?? 0;
    const bar = pct >= 70 ? 'var(--green)' : pct >= 40 ? 'var(--amber)' : 'var(--red)';
    const evidenceHtml = (op.evidence_cited || []).length
      ? `<div class="wr-evidence"><span class="wr-ev-label">Evidence:</span>
         ${op.evidence_cited.map(e => `<span class="wr-ev-tag">${esc(e)}</span>`).join('')}</div>` : '';
    const concernsHtml = (op.concerns || []).length
      ? `<div class="wr-concerns">${op.concerns.map(c => `<span class="wr-concern-tag">${esc(c)}</span>`).join('')}</div>` : '';

    return `
      <div class="wr-agent-full" style="--agent-color:${op.color ? `var(--${op.color})` : '#818cf8'}">
        <div class="wr-agent-header">
          <span class="wr-agent-icon">${esc(op.icon)}</span>
          <span class="wr-agent-name">${esc(op.agent)}</span>
          <span class="wr-agent-conf" style="color:${bar}" data-target="${pct}">0%</span>
        </div>
        <div class="wr-agent-finding">${esc(op.finding)}</div>
        ${evidenceHtml}
        ${op.recommendation ? `<div class="wr-agent-rec">→ ${esc(op.recommendation)}</div>` : ''}
        ${concernsHtml}
      </div>`;
  }).join('');

  const card = upsertCard('card-warroom', 'flow-card flow-warroom expanded', `
    <div class="flow-card-header">
      <span class="flow-icon">🤖</span>
      <span class="flow-title">Multi-Agent War Room</span>
      <span class="flow-count">${opinions.length} specialists</span>
    </div>
    <div class="flow-card-body wr-expanded-grid">${agentsHtml}</div>`);

  requestAnimationFrame(() => {
    card.querySelectorAll('.wr-agent-conf[data-target]').forEach(el => {
      animateCounter(el, 0, parseInt(el.dataset.target)||0, 800);
    });
  });
}

// ── Render: Decision Engine ────────────────────────────────────────────────
function renderDecisionRunning() {
  upsertCard('card-decision', 'flow-card flow-decision running', `
    <div class="flow-card-header">
      <span class="flow-icon">🧠</span>
      <span class="flow-title">LLM Decision Engine</span>
      <span class="flow-count">Analysing…</span>
    </div>
    <div class="flow-card-body">
      <div class="decision-thinking">Synthesizing agent opinions and evidence…</div>
    </div>`);
}

function renderDecisionCard(data) {
  // Parse full decision detail:
  // "Verdict: {root_cause} | Confidence: X%(capped note) | Evidence: Y/100 | Action: Z | Command: cmd"
  const confMatch = data.detail.match(/Confidence:\s*(\d+)%/);
  const actionMatch = data.detail.match(/Action:\s*(\w+)/);
  const verdictMatch = data.detail.match(/Verdict:\s*(.+?)\s*\|/);
  const evidenceMatch = data.detail.match(/Evidence:\s*(\d+)\/100/);
  const cmdMatch = data.detail.match(/Command:\s*(.+?)$/);

  const conf = confMatch ? parseInt(confMatch[1]) : 0;
  const action = actionMatch ? actionMatch[1] : 'unknown';
  const verdict = verdictMatch ? verdictMatch[1].trim() : '';
  const evidenceScore = evidenceMatch ? parseInt(evidenceMatch[1]) : null;
  const command = cmdMatch ? cmdMatch[1].trim() : '';
  const confColor = conf >= 70 ? 'var(--green)' : conf >= 40 ? 'var(--amber)' : 'var(--red)';

  const verdictHtml = verdict
    ? `<div class="decision-verdict">${esc(verdict)}</div>` : '';

  const evidenceHtml = evidenceScore != null
    ? `<div class="decision-evidence-score">
        <span class="decision-label">Evidence Strength</span>
        <div class="confidence-bar-wrap"><div class="confidence-bar" style="width:${evidenceScore}%;background:${evidenceScore >= 60 ? 'var(--green)' : 'var(--amber)'}"></div></div>
        <span class="decision-score-val">${evidenceScore}/100</span>
       </div>` : '';

  const commandHtml = command
    ? `<div class="decision-command">
        <span class="decision-label">Command</span>
        <code class="decision-cmd">${esc(command)}</code>
       </div>` : '';

  const card = upsertCard('card-decision', 'flow-card flow-decision', `
    <div class="flow-card-header">
      <span class="flow-icon">🧠</span>
      <span class="flow-title">LLM Decision Engine</span>
      <span class="flow-conf" style="color:${confColor}" data-target="${conf}">0%</span>
    </div>
    <div class="flow-card-body">
      <div class="decision-action">
        <span class="action-badge action-${action}">${action.toUpperCase()}</span>
      </div>
      ${verdictHtml}
      ${evidenceHtml}
      ${commandHtml}
    </div>`);

  requestAnimationFrame(() => {
    const el = card.querySelector('.flow-conf[data-target]');
    if (el) animateCounter(el, 0, conf, 800);
  });
}

// ── Render: RCA Detail (after decision) ────────────────────────────────────
function renderRCADetail(rca) {
  const pct = rca.confidence ?? 0;
  const bar = pct >= 70 ? 'var(--green)' : pct >= 40 ? 'var(--amber)' : 'var(--red)';

  const pointsHtml = (rca.root_cause_points || []).length
    ? `<ul class="rca-points">${rca.root_cause_points.map(p => `<li>${esc(p)}</li>`).join('')}</ul>` : '';

  const factors = rca.contributing_factors || [];
  const factorsHtml = factors.length
    ? `<div class="rca-factors"><span class="rca-label">Contributing factors</span>
       ${factors.map(f => `<span class="factor-tag">${esc(f)}</span>`).join('')}</div>` : '';

  const blast = rca.blast_radius;
  const blastHtml = (blast && blast.toLowerCase() !== 'none')
    ? `<div class="rca-blast">⚡ Blast radius: ${esc(blast)}</div>` : '';

  const card = upsertCard('card-rca', 'flow-card flow-rca', `
    <div class="flow-card-header">
      <span class="flow-icon">📋</span>
      <span class="flow-title">Root Cause Analysis</span>
    </div>
    <div class="flow-card-body">
      <div class="rca-summary">${esc(rca.root_cause || '—')}</div>
      ${pointsHtml}
      ${factorsHtml}
      ${blastHtml}
      <div class="rca-conf-bar">
        <div class="confidence-bar-wrap"><div class="confidence-bar" style="width:0%;background:${bar}"></div></div>
        <span class="confidence-label" data-target="${pct}">0% confidence</span>
      </div>
    </div>`);

  requestAnimationFrame(() => {
    const barEl = card.querySelector('.confidence-bar');
    if (barEl) barEl.style.width = `${pct}%`;
    const labelEl = card.querySelector('.confidence-label[data-target]');
    if (labelEl) animateCounter(labelEl, 0, pct, 1000, '% confidence');
  });
}

// ── Render: Remediation card ───────────────────────────────────────────────
function renderRemediationCard(data) {
  const icon = data.status === 'success' ? '✅' : data.status === 'running' ? '⚡' : '❌';
  const title = data.status === 'running' ? 'Executing Remediation…' : 'Remediation';

  upsertCard('card-remediation', `flow-card flow-remediation ${data.status}`, `
    <div class="flow-card-header">
      <span class="flow-icon">${icon}</span>
      <span class="flow-title">${title}</span>
    </div>
    <div class="flow-card-body">
      <div class="remediation-detail">${esc(data.detail)}</div>
    </div>`);
}

// ── Render: Info card (generic) ────────────────────────────────────────────
function renderInfoCard(id, icon, title, detail) {
  upsertCard(`card-${id}`, 'flow-card flow-info', `
    <div class="flow-card-header">
      <span class="flow-icon">${icon}</span>
      <span class="flow-title">${esc(title)}</span>
    </div>
    <div class="flow-card-body"><div class="flow-detail">${esc(detail)}</div></div>`);
}

// ── Render: Generic timeline item ──────────────────────────────────────────
function renderGenericItem(data) {
  const icon = {running:'↻', success:'✓', error:'✗', info:'i'}[data.status] || '·';
  const item = document.createElement('div');
  item.className = 'flow-card flow-generic';
  item.innerHTML = `
    <div class="flow-card-header">
      <span class="flow-icon-sm ${data.status}">${icon}</span>
      <span class="flow-title-sm">${esc(data.step)}</span>
      <span class="flow-time">${fmtTime(data.timestamp)}</span>
    </div>
    <div class="flow-card-body"><div class="flow-detail">${esc(data.detail)}</div></div>`;
  appendToTimeline(item);
}

// ── Render: Closing summary ────────────────────────────────────────────────
function renderClosingSummary(status, seconds) {
  elApprovalBanner.classList.remove('visible');
  removeById('card-closing');

  const resolved = status === 'resolved';
  const skipped = status === 'skipped';
  const icon = resolved ? '✅' : skipped ? '⏭' : '❌';
  const title = resolved
    ? `Incident Resolved in ${fmtElapsed(seconds)}`
    : skipped ? 'Remediation Skipped' : 'Resolution Failed';
  const sub = resolved
    ? 'Autonomous remediation successful. System is healthy.'
    : skipped ? 'Manual review recommended.'
    : 'Manual intervention required.';

  // 🎉 Celebration on resolve!
  if (resolved) {
    const tc = document.getElementById('toast-container');
    if (tc) tc.innerHTML = '';
    triggerCelebration();
    const svc = incidentMeta?.alert?.service || 'Service';
    showResolvedToast(svc, seconds);
  }

  const jiraHtml = jiraKey
    ? `<div class="close-jira">
         <span class="close-jira-label">Jira:</span>
         ${jiraUrl
           ? `<a href="${esc(jiraUrl)}" target="_blank" class="close-jira-link">${esc(jiraKey)} — ${resolved ? 'Done' : 'Open'} ↗</a>`
           : `<span>${esc(jiraKey)}</span>`}
       </div>` : '';

  const card = document.createElement('div');
  card.id = 'card-closing';
  card.className = `flow-card flow-closing ${resolved ? 'success' : 'fail'}`;
  card.innerHTML = `
    <div class="close-icon">${icon}</div>
    <div class="close-title">${title}</div>
    <div class="close-sub">${sub}</div>
    ${jiraHtml}
    <div class="close-actions">
      ${resolved ? `<button class="btn btn-ghost" onclick="generatePostmortem()">📄 Generate Postmortem</button>` : ''}
    </div>`;
  appendToTimeline(card);

  // Also update the banner for backward compat
  elResBanner.className = `resolved-banner visible ${resolved ? 'resolved' : 'failed'}`;
  elResIcon.textContent = icon;
  elResTitle.textContent = title;
  elResSub.textContent = sub;
  elBadge.innerHTML = badgeHtml(status);
  if (resolved) $('btn-postmortem').style.display = 'inline-flex';
}

// ── Approval ───────────────────────────────────────────────────────────────
function showApprovalBanner(action, confidence, command) {
  const rawAct = (action||'').replace(/^RemediationType\./i,'').toLowerCase() || 'remediation';
  const conf = confidence != null ? ` (confidence: ${confidence}%)` : '';
  elApprovalBody.innerHTML = `<div>Recommended: <strong>${esc(rawAct)}</strong>${conf}</div>`;
  const cmdEl = $('approval-command');
  if (command) {
    cmdEl.innerHTML = `<span class="approval-cmd-label">Command:</span><code>${esc(command)}</code>`;
    cmdEl.style.display = '';
  } else { cmdEl.style.display = 'none'; }
  elApprovalBanner.classList.add('visible');
  elBtnApprove.disabled = false;
  elBtnSkip.disabled = false;
}

async function handleApproval(action) {
  if (!activeIncidentId) return;
  elBtnApprove.disabled = true;
  elBtnSkip.disabled = true;
  try {
    const r = await fetch(`${API}/incidents/${activeIncidentId}/action`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action}),
    });
    if (!r.ok) throw new Error(await r.text());
    elApprovalBanner.classList.remove('visible');
    if (action === 'skip') renderClosingSummary('skipped', null);
  } catch(e) {
    alert(`Action failed: ${e}`);
    elBtnApprove.disabled = false;
    elBtnSkip.disabled = false;
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────
function removeById(id) { document.getElementById(id)?.remove(); }

/** Insert or update a card in-place. If the card already exists in the DOM,
 *  replace its innerHTML without moving it. Only append if it's new. */
function upsertCard(id, className, html) {
  let card = document.getElementById(id);
  if (card) {
    card.className = className;
    card.innerHTML = html;
    return card;
  }
  card = document.createElement('div');
  card.id = id;
  card.className = className;
  card.innerHTML = html;
  elTimeline.appendChild(card);
  card.scrollIntoView({behavior:'smooth',block:'nearest'});
  return card;
}

function appendToTimeline(el) {
  elTimeline.appendChild(el);
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
}

function animateCounter(el, start, end, duration, suffix = '%') {
  const t0 = performance.now();
  function tick(now) {
    const p = Math.min((now-t0)/duration, 1);
    const eased = 1 - Math.pow(1-p, 3);
    el.textContent = `${Math.round(start+(end-start)*eased)}${suffix}`;
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Chat ───────────────────────────────────────────────────────────────────
function openChat() {
  $('chat-drawer').classList.add('open');
  $('chat-backdrop').classList.add('open');
  setTimeout(() => $('chat-input')?.focus(), 310);
}
function closeChat() {
  $('chat-drawer')?.classList.remove('open');
  $('chat-backdrop')?.classList.remove('open');
}
function useChip(el) {
  $('chat-chips')?.parentElement.remove();
  $('chat-welcome')?.remove();
  $('chat-input').value = el.textContent;
  sendChat();
}
function buildChatWelcome() {
  const w = document.createElement('div');
  w.id = 'chat-welcome';
  w.className = 'chat-welcome';
  w.innerHTML = `
    <div class="chat-welcome-avatar">🔍</div>
    <div class="chat-welcome-text">Hi! I'm Sherlock, your AI incident assistant.<br/>Ask me anything.</div>
    <div class="chat-chips" id="chat-chips">
      <button class="chat-chip" onclick="useChip(this)">What's the root cause?</button>
      <button class="chat-chip" onclick="useChip(this)">How do I fix this?</button>
      <button class="chat-chip" onclick="useChip(this)">What services are affected?</button>
      <button class="chat-chip" onclick="useChip(this)">Is this a recurring issue?</button>
    </div>`;
  return w;
}

// ── Voice Assistant (Web Speech API) ─────────────────────────────────────
let voiceRecognition = null;
let isListening = false;

function toggleVoice() {
  if (isListening) {
    stopVoice();
  } else {
    startVoice();
  }
}

function startVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert('Speech recognition is not supported in this browser. Please use Chrome or Edge.');
    return;
  }

  voiceRecognition = new SpeechRecognition();
  voiceRecognition.lang = 'en-US';
  voiceRecognition.continuous = true;
  voiceRecognition.interimResults = true;
  voiceRecognition.maxAlternatives = 1;

  voiceRecognition.onstart = function() {
    isListening = true;
    document.getElementById('chat-voice-btn').classList.add('listening');
    document.getElementById('chat-input').placeholder = 'Listening... speak now';
  };

  voiceRecognition.onresult = function(event) {
    let finalTranscript = '';
    let interimTranscript = '';
    for (let i = 0; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        finalTranscript += event.results[i][0].transcript;
      } else {
        interimTranscript += event.results[i][0].transcript;
      }
    }
    document.getElementById('chat-input').value = finalTranscript || interimTranscript;
  };

  voiceRecognition.onend = function() {
    // If still supposed to be listening, restart (handles auto-stop)
    if (isListening) {
      try { voiceRecognition.start(); } catch(e) {}
      return;
    }
    document.getElementById('chat-voice-btn').classList.remove('listening');
    document.getElementById('chat-input').placeholder = 'Ask anything about this incident…';
  };

  voiceRecognition.onerror = function(event) {
    if (event.error === 'no-speech' || event.error === 'aborted') return;
    isListening = false;
    document.getElementById('chat-voice-btn').classList.remove('listening');
    document.getElementById('chat-input').placeholder = 'Ask anything about this incident…';
    console.error('Speech error:', event.error);
    if (event.error === 'not-allowed') {
      alert('Microphone access denied. Please allow microphone permission in browser settings.');
    } else if (event.error === 'network') {
      alert('Speech recognition requires internet connection.');
    }
  };

  voiceRecognition.start();
}

function stopVoice() {
  isListening = false;
  if (voiceRecognition) {
    voiceRecognition.stop();
  }
  document.getElementById('chat-voice-btn').classList.remove('listening');
  var chatInput = document.getElementById('chat-input');
  chatInput.placeholder = 'Ask anything about this incident…';
  // Send the text if there is any
  if (chatInput.value.trim()) {
    sendChat();
  }
}

async function sendChat() {
  if (!activeIncidentId) return;
  const input = $('chat-input');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  $('chat-welcome')?.remove();
  appendChatBubble('user', q);
  const thinking = appendChatBubble('sherlock', null, true);
  try {
    const r = await fetch(`${API}/incidents/${activeIncidentId}/chat`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:q}),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    thinking.innerHTML = '';
    thinking.textContent = d.answer;
    thinking.closest('.chat-msg').classList.remove('thinking');
  } catch(e) {
    thinking.innerHTML = '';
    thinking.textContent = `Error: ${e}`;
    thinking.closest('.chat-msg').classList.remove('thinking');
    thinking.closest('.chat-msg').classList.add('error');
  }
  input.focus();
}

function appendChatBubble(role, text, isThinking=false) {
  const msgs = $('chat-messages');
  const el = document.createElement('div');
  el.className = `chat-msg chat-msg-${role}${isThinking?' thinking':''}`;
  const av = role === 'user' ? '👤' : '🔍';
  const label = role === 'user' ? 'You' : 'Sherlock';
  el.innerHTML = `
    <div class="chat-msg-avatar">${av}</div>
    <div class="chat-bubble-wrap">
      <div class="chat-label">${label}</div>
      ${isThinking
        ? '<div class="chat-text"><div class="typing-indicator"><span></span><span></span><span></span></div></div>'
        : `<div class="chat-text">${esc(text||'')}</div>`}
    </div>`;
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el.querySelector('.chat-text');
}

// ── Postmortem ─────────────────────────────────────────────────────────────
async function generatePostmortem() {
  if (!activeIncidentId) return;
  const overlay = $('pm-overlay');
  const content = $('pm-body');
  content.innerHTML = '<div class="pm-loading">⏳ Generating postmortem with AI…</div>';
  overlay.style.display = 'flex';
  try {
    const r = await fetch(`${API}/incidents/${activeIncidentId}/postmortem`);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    let md = (d.postmortem||'').replace(/^```(?:markdown)?\s*\n?/i,'').replace(/\n?```\s*$/i,'');
    content.innerHTML = `<div class="pm-text">${renderMarkdown(md)}</div>`;
  } catch(e) { content.innerHTML = `<div class="pm-loading">Error: ${e}</div>`; }
}

function renderMarkdown(md) {
  if (!md) return '';
  const lines = md.split('\n');
  let html = '', inTable = false, inList = false, listType = 'ul';
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    if (/^---+\s*$/.test(line)) { if (inList){html+=`</${listType}>`;inList=false;} if(inTable){html+='</tbody></table>';inTable=false;} html+='<hr>'; continue; }
    const hM = line.match(/^(#{1,3})\s+(.+)/);
    if (hM) { if(inList){html+=`</${listType}>`;inList=false;} if(inTable){html+='</tbody></table>';inTable=false;} html+=`<h${hM[1].length}>${inlineFmt(hM[2])}</h${hM[1].length}>`; continue; }
    if (line.trim().startsWith('|')&&line.trim().endsWith('|')) {
      const cells = line.split('|').slice(1,-1).map(c=>c.trim());
      if (cells.every(c=>/^[-:]+$/.test(c))) continue;
      if (!inTable) { if(inList){html+=`</${listType}>`;inList=false;} html+='<table><thead><tr>'+cells.map(c=>`<th>${inlineFmt(c)}</th>`).join('')+'</tr></thead><tbody>'; inTable=true; }
      else { html+='<tr>'+cells.map(c=>`<td>${inlineFmt(c)}</td>`).join('')+'</tr>'; }
      continue;
    }
    if (inTable&&!line.trim().startsWith('|')) { html+='</tbody></table>'; inTable=false; }
    if (/^\s*[-*]\s+/.test(line)) { if(!inList||listType!=='ul'){if(inList)html+=`</${listType}>`;html+='<ul>';inList=true;listType='ul';} html+=`<li>${inlineFmt(line.replace(/^\s*[-*]\s+/,''))}</li>`; continue; }
    if (/^\s*\d+\.\s+/.test(line)) { if(!inList||listType!=='ol'){if(inList)html+=`</${listType}>`;html+='<ol>';inList=true;listType='ol';} html+=`<li>${inlineFmt(line.replace(/^\s*\d+\.\s+/,''))}</li>`; continue; }
    if (inList) { html+=`</${listType}>`; inList=false; }
    if (!line.trim()) continue;
    html+=`<p>${inlineFmt(line)}</p>`;
  }
  if (inList) html+=`</${listType}>`;
  if (inTable) html+='</tbody></table>';
  return html;
}
function inlineFmt(t) {
  return esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

async function copyPostmortem() {
  const text = $('pm-body')?.innerText || '';
  await navigator.clipboard.writeText(text);
  const btn = $('pm-copy-btn');
  if (btn) { btn.textContent = '✅ Copied'; setTimeout(() => { btn.textContent = '📋 Copy'; }, 2000); }
}
function closePostmortem() { $('pm-overlay').style.display = 'none'; }

// ── Logo reload animation ──────────────────────────────────────────────────
const elLogoIcon = document.querySelector('.logo-icon-wrap');
function startReloadAnimation() { elLogoIcon?.classList.add('reloading'); }
function stopReloadAnimation()  { elLogoIcon?.classList.remove('reloading'); }

// ── Button wiring ──────────────────────────────────────────────────────────
elBtnApprove?.addEventListener('click', () => handleApproval('approve'));
elBtnSkip?.addEventListener('click',    () => handleApproval('skip'));

elRefreshBtn?.addEventListener('click', () => { loadIncidents(); checkHealth(); });

elTriggerBtn?.addEventListener('click', async () => {
  elTriggerBtn.disabled = true;
  elTriggerBtn.innerHTML = '⏳ Scanning…';
  startReloadAnimation();
  try {
    const r = await fetch(`${API}/scan`, { method:'POST', headers:{'Content-Type':'application/json'} });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    await loadIncidents();
    if (d.incidents_created?.length) await selectIncident(d.incidents_created[0].id);
  } catch(e) { alert(`Scan failed: ${e}`); }
  finally {
    stopReloadAnimation();
    elTriggerBtn.disabled = false;
    elTriggerBtn.innerHTML = '<span class="trigger-bolt">⚡</span> Analyse';
  }
});

// ── Init ───────────────────────────────────────────────────────────────────
checkHealth();
loadIncidents();
setInterval(checkHealth,   30_000);
setInterval(loadIncidents, 10_000);

// ═══════════════════════════════════════════════════════════════════════════
// CATCHY EFFECTS — Confetti, Toasts, Radar
// ═══════════════════════════════════════════════════════════════════════════

// ── Confetti Celebration ───────────────────────────────────────────────────
function triggerCelebration() {
  const canvas = document.getElementById('confetti-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;

  const colors = ['#a78bfa','#7c3aed','#34d399','#22d3ee','#fbbf24','#f472b6','#818cf8','#c084fc'];
  const particles = [];

  for (let i = 0; i < 120; i++) {
    particles.push({
      x: canvas.width / 2 + (Math.random() - 0.5) * 200,
      y: canvas.height / 2,
      vx: (Math.random() - 0.5) * 18,
      vy: Math.random() * -18 - 4,
      w: Math.random() * 8 + 4,
      h: Math.random() * 6 + 3,
      color: colors[Math.floor(Math.random() * colors.length)],
      rotation: Math.random() * 360,
      rotSpeed: (Math.random() - 0.5) * 12,
      gravity: 0.35 + Math.random() * 0.15,
      opacity: 1,
      decay: 0.008 + Math.random() * 0.006
    });
  }

  function animate() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    let alive = false;
    for (const p of particles) {
      if (p.opacity <= 0) continue;
      alive = true;
      p.x += p.vx; p.y += p.vy;
      p.vy += p.gravity; p.vx *= 0.99;
      p.rotation += p.rotSpeed;
      p.opacity -= p.decay;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rotation * Math.PI / 180);
      ctx.globalAlpha = Math.max(0, p.opacity);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (alive) requestAnimationFrame(animate);
    else ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  animate();

  // Screen flash
  const flash = document.createElement('div');
  flash.className = 'screen-flash';
  document.body.appendChild(flash);
  setTimeout(() => flash.remove(), 900);
}

// ── Toast Notifications ────────────────────────────────────────────────────
function showToast(type, icon, title, subtitle, duration = 3000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  // Clear previous toasts
  container.innerHTML = '';

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icon}</span>
    <div class="toast-text">
      <div class="toast-title">${title}</div>
      ${subtitle ? `<div class="toast-sub">${subtitle}</div>` : ''}
    </div>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('out');
    setTimeout(() => toast.remove(), 400);
  }, duration);
}

function showResolvedToast(service, seconds) {
  showToast('resolved', '✅', 'INCIDENT RESOLVED',
    `${service} healed autonomously in ${fmtElapsed(seconds)}`, 3000);
}

function showAlertToast(service) {
  showToast('alert', '🚨', 'INCIDENT DETECTED',
    `${service} — investigation started`, 3000);
}

// ═══════════════════════════════════════════════════════════════════════════
// SETTINGS PANEL
// ═══════════════════════════════════════════════════════════════════════════

function openSettings() {
  document.getElementById('settings-overlay').style.display = 'flex';
  loadSettings();
  loadKubeContexts();
  loadOllamaModels();
}

function closeSettings() {
  document.getElementById('settings-overlay').style.display = 'none';
  document.getElementById('settings-save-status').textContent = '';
  document.getElementById('settings-save-status').className = 'settings-save-status';
}

function switchTab(name) {
  document.querySelectorAll('.settings-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.settings-pane').forEach(p =>
    p.classList.toggle('active', p.id === `tab-${name}`));
}

// ── Load current settings from backend ────────────────────────────────────
async function loadSettings() {
  try {
    const r = await fetch(`${API}/settings`);
    const d = await r.json();

    // Ollama
    setVal('s-ollama-url', d.ollama_base_url || '');
    // model is set after models load
    window._currentOllamaModel = d.ollama_model || '';

    // Kubernetes
    window._currentKubeContext = d.kube_context || '';
    setVal('s-namespace', d.default_namespace || 'default');

    // Behaviour
    const approvalEl = document.getElementById('s-approval-mode');
    if (approvalEl) approvalEl.checked = (d.approval_mode === 'true' || d.approval_mode === true);
    const threshold = parseInt(d.auto_approve_threshold) || 90;
    const threshEl = document.getElementById('s-threshold');
    if (threshEl) { threshEl.value = threshold; }
    const dispEl = document.getElementById('threshold-display');
    if (dispEl) dispEl.textContent = threshold + '%';

    // Teams (masked)
    setVal('s-teams-url', d.teams_webhook_url || '');

    // Jira
    const jiraEnabledEl = document.getElementById('s-jira-enabled');
    if (jiraEnabledEl) {
      jiraEnabledEl.checked = (d.jira_enabled === 'true' || d.jira_enabled === true);
      toggleJiraFields();
    }
    setVal('s-jira-url', d.jira_url || '');
    setVal('s-jira-email', d.jira_email || '');
    setVal('s-jira-token', d.jira_api_token || '');
    setVal('s-jira-project', d.jira_project_key || 'KS');
    const jiraTypeEl = document.getElementById('s-jira-type');
    if (jiraTypeEl) jiraTypeEl.value = d.jira_issue_type || 'Task';
  } catch (e) {
    console.warn('loadSettings failed:', e);
  }
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

// ── Load available Ollama models ───────────────────────────────────────────
async function loadOllamaModels() {
  const sel = document.getElementById('s-ollama-model');
  if (!sel) return;
  try {
    const r = await fetch(`${API}/settings/ollama-models`);
    const d = await r.json();
    const current = window._currentOllamaModel
      || document.getElementById('s-ollama-url')?.value
      || 'llama3.2';
    sel.innerHTML = '';
    const models = d.models && d.models.length
      ? d.models
      : [current || 'llama3.2'];
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === (window._currentOllamaModel || current)) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!d.models || !d.models.length) {
      const opt = document.createElement('option');
      opt.value = current;
      opt.textContent = current + ' (Ollama not running)';
      sel.appendChild(opt);
    }
  } catch {
    sel.innerHTML = `<option value="${window._currentOllamaModel || 'llama3.2'}">${window._currentOllamaModel || 'llama3.2'} (offline)</option>`;
  }
}

// ── Load available kubectl contexts ───────────────────────────────────────
async function loadKubeContexts() {
  const sel = document.getElementById('s-kube-context');
  if (!sel) return;
  try {
    const r = await fetch(`${API}/settings/kube-contexts`);
    const d = await r.json();
    sel.innerHTML = '';
    const contexts = d.contexts && d.contexts.length ? d.contexts : ['default'];
    const current = window._currentKubeContext || d.current || '';
    contexts.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c;
      opt.textContent = c === d.current ? `${c}  ✓` : c;
      if (c === current) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!d.contexts || !d.contexts.length) {
      const opt = document.createElement('option');
      opt.value = current;
      opt.textContent = `${current || 'No context found'} (check kubectl)`;
      sel.appendChild(opt);
    }
  } catch {
    const sel2 = document.getElementById('s-kube-context');
    if (sel2) sel2.innerHTML = `<option value="">Could not reach kubectl</option>`;
  }
}

// ── Save settings ──────────────────────────────────────────────────────────
async function saveSettings() {
  const status = document.getElementById('settings-save-status');
  status.textContent = 'Saving…';
  status.className = 'settings-save-status';

  const approvalEl = document.getElementById('s-approval-mode');
  const jiraEnabledEl = document.getElementById('s-jira-enabled');

  const payload = {
    ollama_base_url:        document.getElementById('s-ollama-url')?.value || null,
    ollama_model:           document.getElementById('s-ollama-model')?.value || null,
    kube_context:           document.getElementById('s-kube-context')?.value || null,
    default_namespace:      document.getElementById('s-namespace')?.value || null,
    approval_mode:          approvalEl ? String(approvalEl.checked) : null,
    auto_approve_threshold: document.getElementById('s-threshold')?.value || null,
    teams_webhook_url:      document.getElementById('s-teams-url')?.value || null,
    jira_enabled:           jiraEnabledEl ? String(jiraEnabledEl.checked) : null,
    jira_url:               document.getElementById('s-jira-url')?.value || null,
    jira_email:             document.getElementById('s-jira-email')?.value || null,
    jira_api_token:         document.getElementById('s-jira-token')?.value || null,
    jira_project_key:       document.getElementById('s-jira-project')?.value || null,
    jira_issue_type:        document.getElementById('s-jira-type')?.value || null,
  };

  // Remove nulls
  Object.keys(payload).forEach(k => payload[k] === null && delete payload[k]);

  try {
    const r = await fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.saved) {
      status.textContent = `✅ Saved (${d.updated_keys.length} settings updated)`;
      status.className = 'settings-save-status ok';
      // Refresh health bar to reflect new model/context
      checkHealth();
    } else {
      status.textContent = d.message || 'No changes detected.';
      status.className = 'settings-save-status';
    }
  } catch (e) {
    status.textContent = '❌ Save failed — is KIRA running?';
    status.className = 'settings-save-status error';
  }
}

// ── Test Jira connection ───────────────────────────────────────────────────
async function testJira() {
  const btn = document.getElementById('btn-test-jira');
  const statusEl = document.getElementById('jira-test-status');
  btn.disabled = true;
  statusEl.textContent = 'Testing…';
  statusEl.className = 'test-status';

  try {
    const r = await fetch(`${API}/settings/test/jira`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jira_url:       document.getElementById('s-jira-url')?.value,
        jira_email:     document.getElementById('s-jira-email')?.value,
        jira_api_token: document.getElementById('s-jira-token')?.value,
      }),
    });
    const d = await r.json();
    statusEl.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
    statusEl.className = `test-status ${d.ok ? 'ok' : 'error'}`;
  } catch {
    statusEl.textContent = '❌ Could not reach backend';
    statusEl.className = 'test-status error';
  } finally {
    btn.disabled = false;
  }
}

// ── Test Teams connection ──────────────────────────────────────────────────
async function testTeams() {
  const btn = document.getElementById('btn-test-teams');
  const statusEl = document.getElementById('teams-test-status');
  btn.disabled = true;
  statusEl.textContent = 'Sending…';
  statusEl.className = 'test-status';

  try {
    const r = await fetch(`${API}/settings/test/teams`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        teams_webhook_url: document.getElementById('s-teams-url')?.value,
      }),
    });
    const d = await r.json();
    statusEl.textContent = (d.ok ? '✅ ' : '❌ ') + d.message;
    statusEl.className = `test-status ${d.ok ? 'ok' : 'error'}`;
  } catch {
    statusEl.textContent = '❌ Could not reach backend';
    statusEl.className = 'test-status error';
  } finally {
    btn.disabled = false;
  }
}

// ── Toggle Jira field visibility ───────────────────────────────────────────
function toggleJiraFields() {
  const enabled = document.getElementById('s-jira-enabled')?.checked;
  const fields = document.getElementById('jira-fields');
  if (fields) fields.classList.toggle('disabled', !enabled);
}

