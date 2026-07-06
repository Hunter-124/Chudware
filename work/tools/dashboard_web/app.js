/* ============================================================
   AI-CAD Mission Control — app.js
   Plain-language, human-first rendering of the task board.
   Views: Overview (stats / needs-you / progress) and Map
   (swim-lane dependency graph with pan/zoom). One drawer.
   ============================================================ */

'use strict';

// ---------------- vocabulary: machine words -> human words ----------------

const LANE_META = {
  'modeling-core':       { name: '3D Modeling Core',            icon: '🧊' },
  'assemblies-drawings': { name: 'Assemblies & Drawings',       icon: '📐' },
  'cam':                 { name: 'CAM & Machining',             icon: '🛠️' },
  'ecad-firmware':       { name: 'Electronics & Firmware',      icon: '⚡' },
  'simulation':          { name: 'Simulation & Analysis',       icon: '🌊' },
  'parts-sourcing':      { name: 'Parts Library & Sourcing',    icon: '🔩' },
  'ai-copilot':          { name: 'AI Copilot',                  icon: '🤖' },
  'app-gui':             { name: 'App & Interface',             icon: '🖥️' },
  'plm-projects':        { name: 'Projects & Version Control',  icon: '🗂️' },
  'platform-infra':      { name: 'Platform & Infrastructure',   icon: '🧱' },
  'ml-flywheel':         { name: 'ML Training',                 icon: '🧠' },
  '(untagged)':          { name: 'Untagged',                    icon: '·' },
};
const LANE_ORDER_FALLBACK = Object.keys(LANE_META);

const KIND_LABEL = { fix: 'FIX', overhaul: 'REDO', new: 'NEW' };

// display status (computed, richer than raw status)
const STATUS_META = {
  ready:   { label: 'Ready to start',    color: 'var(--ready)',   soft: 'var(--ready-soft)' },
  waiting: { label: 'Waiting on others', color: 'var(--waiting)', soft: 'var(--waiting-soft)' },
  doing:   { label: 'In progress',       color: 'var(--doing)',   soft: 'var(--doing-soft)' },
  review:  { label: 'Needs review',      color: 'var(--review)',  soft: 'var(--review-soft)' },
  blocked: { label: 'Stuck',             color: 'var(--stuck)',   soft: 'var(--stuck-soft)' },
  done:    { label: 'Done',              color: 'var(--done)',    soft: 'var(--done-soft)' },
  backlog: { label: 'Someday',           color: 'var(--someday)', soft: 'var(--someday-soft)' },
  paused:  { label: 'Paused',            color: 'var(--someday)', soft: 'var(--someday-soft)' },
};

// ---------------- state ----------------

const S = {
  view: 'overview',
  tasks: [], byId: {}, dependents: {},
  roadblocks: { review_queue: [], blocked: [], human_owned: [], questions: [] },
  lanes: [],
  assign: {},                 // lane -> { owner, branch, goal }
  issues: { issues: [], open_count: 0, fetched_ts: null },
  assignLane: null,           // lane currently open in the assign modal
  // codebase node graph
  graph: null, gLoaded: false, gLoading: false,
  gx: 0, gy: 0, gscale: 1, gEdges: true, gSearch: '',
  gGrid: null, gCellByComm: null, gHover: -1,
  hash: '',
  lastFetch: 0,
  // map
  tx: 40, ty: 20, scale: 1, fitted: false,
  laneFilter: null, search: '',
  hideDone: true, showPaused: false,
  layout: null, selected: null,
  // drawer steering
  drawerId: null, steerPriority: 5,
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------------- data ----------------

const isPaused = (t) => ['yes', 'true', '1'].includes(String(t.deferred || 'no').toLowerCase());
const prio = (t) => parseInt(t.priority || 5, 10) || 5;

function dispStatus(t) {
  if (isPaused(t)) return 'paused';
  if (t.status === 'todo') {
    const deps = t.depends_on || [];
    return deps.every((d) => (S.byId[d] || {}).status === 'done') ? 'ready' : 'waiting';
  }
  return STATUS_META[t.status] ? t.status : 'waiting';
}
const laneOf = (t) => LANE_META[t.lane] ? t.lane : '(untagged)';
const laneName = (l) => (LANE_META[l] || LANE_META['(untagged)']).name;

async function fetchAll(force) {
  try {
    const [board, roadblocks, lanes, assign, issues] = await Promise.all([
      fetch('/api/board.json').then((r) => r.json()),
      fetch('/api/roadblocks.json').then((r) => r.json()),
      fetch('/api/lanes.json').then((r) => r.json()),
      fetch('/api/assignments.json').then((r) => r.json()).catch(() => []),
      fetch('/api/issues.json').then((r) => r.json()).catch(() => ({ issues: [] })),
    ]);
    const hash = JSON.stringify([board, roadblocks, lanes, assign, issues]);
    S.lastFetch = Date.now();
    if (hash === S.hash && !force) return;
    S.hash = hash;
    S.tasks = board;
    S.byId = Object.fromEntries(board.map((t) => [t.id, t]));
    S.dependents = {};
    for (const t of board) for (const d of (t.depends_on || []))
      (S.dependents[d] = S.dependents[d] || []).push(t.id);
    S.roadblocks = roadblocks;
    S.lanes = lanes;
    S.assign = Object.fromEntries((assign || []).map((a) => [a.lane, a]));
    S.issues = issues || { issues: [] };
    S.layout = null;
    renderAll();
  } catch (e) {
    $('#refresh-note').textContent = 'refresh failed — is the repo ok?';
  }
}

function renderAll() {
  renderOverview();
  if (S.view === 'map') renderMap();
  if (S.drawerId) fillDrawerHead(S.byId[S.drawerId]); // keep drawer fresh
}

// ---------------- overview: hero + stats ----------------

function renderOverview() {
  const tasks = S.tasks;
  const doing = tasks.filter((t) => t.status === 'doing' && !isPaused(t));
  const ready = tasks.filter((t) => dispStatus(t) === 'ready');
  const done = tasks.filter((t) => t.status === 'done');
  const rb = S.roadblocks;
  const needsYou = rb.review_queue.length + rb.blocked.length + rb.human_owned.length;
  const nQuestions = (rb.questions || []).reduce((a, q) => a + (q.open || 0), 0);

  // hero sentence
  const owners = [...new Set(doing.map((t) => t.owner))];
  const topLane = [...S.lanes].filter((l) => l.total > l.done && l.lane !== '(untagged)')
    .sort((a, b) => b.pct_done - a.pct_done)[0];
  let line = doing.length
    ? `<b>${owners.length}</b> agent${owners.length === 1 ? '' : 's'} working on <b>${doing.length}</b> task${doing.length === 1 ? '' : 's'} right now.`
    : `No agents are working right now.`;
  if (needsYou) line += ` <b>${needsYou}</b> thing${needsYou === 1 ? '' : 's'} need${needsYou === 1 ? 's' : ''} you.`;
  else line += ` Nothing is waiting on you.`;
  if (topLane) line += ` Closest to finished: <b>${esc(laneName(topLane.lane))}</b> (${topLane.pct_done}%).`;
  $('#hero-line').innerHTML = line;

  // stat tiles
  $('#stat-row').innerHTML = `
    <div class="stat warn clickable" id="stat-needs-you">
      <div class="stat-num">${needsYou}</div>
      <div class="stat-label">need you</div>
      <div class="stat-hint">${needsYou ? 'review, merge, or answer' : 'all clear ✓'}${nQuestions ? ` · ${nQuestions} open question${nQuestions === 1 ? '' : 's'}` : ''}</div>
    </div>
    <div class="stat busy">
      <div class="stat-num">${doing.length}</div>
      <div class="stat-label">in progress</div>
      <div class="stat-hint">${esc(owners.slice(0, 3).join(', '))}${owners.length > 3 ? ` +${owners.length - 3}` : ''}&nbsp;</div>
    </div>
    <div class="stat">
      <div class="stat-num">${ready.length}</div>
      <div class="stat-label">ready to be picked up</div>
      <div class="stat-hint">agents claim these automatically</div>
    </div>
    <div class="stat good">
      <div class="stat-num">${done.length}<span style="font-size:18px;color:var(--text-faint)">/${tasks.length}</span></div>
      <div class="stat-label">finished</div>
      <div class="stat-hint">${Math.round((done.length / Math.max(tasks.length, 1)) * 100)}% of everything planned</div>
    </div>`;
  $('#stat-needs-you').onclick = () =>
    $('#needs-you-section').scrollIntoView({ behavior: 'smooth' });

  renderNeedsYou();
  renderIssues();
  renderLaneRows();
}

// ---------------- overview: hot issues ----------------

function renderIssues() {
  const issues = (S.issues && S.issues.issues) || [];
  const el = $('#issue-rows');
  if (!issues.length) {
    el.innerHTML = `<div class="empty-state">No open issues 🎉 — nothing on fire right now.</div>`;
    return;
  }
  el.innerHTML = issues.slice(0, 12).map((i) => {
    const labels = (i.label_names || []).map((n) =>
      `<span class="ilabel ${n === 'bug' ? 'bug' : ''}">${esc(n)}</span>`).join('');
    const who = (i.assignee_logins || []).length
      ? `<span class="iassignee">👤 ${esc(i.assignee_logins.join(', '))}</span>`
      : `<span class="iassignee unassigned">→ lane/misc-fixes</span>`;
    return `
    <a class="issue ${i.is_bug ? 'bug' : ''}" href="${esc(i.url)}" target="_blank" rel="noopener">
      <span class="inum">#${i.number}</span>
      <span class="ititle">${esc(i.title)}</span>
      <span class="imeta">${labels}${who}</span>
    </a>`;
  }).join('');
}

// ---------------- overview: needs-you cards ----------------

function impactSentence(r) {
  const n = (r.blocks || []).length;
  const v = r.verdict || '';
  if (v.startsWith('HIGH-IMPACT'))
    return `<span class="imp-high">⚑ Important:</span> doing this frees up ${n} other task${n === 1 ? '' : 's'}.`;
  if (v.startsWith('low-impact'))
    return `<span class="imp-low">Nothing important is waiting on this — safe to skip for now.</span>`;
  return n ? `${n} other task${n === 1 ? '' : 's'} will unlock after this.` : `Nothing else depends on this.`;
}

function renderNeedsYou() {
  const rb = S.roadblocks;
  const items = [];
  for (const r of rb.review_queue) items.push({ r, icon: '✅', cls: 'merge', act: `Finished by ${esc(r.owner)} — needs a human to review & merge.` });
  for (const r of rb.blocked) items.push({ r, icon: '🚧', cls: 'stuck', act: 'Marked stuck — needs a human decision.' });
  for (const r of rb.human_owned) items.push({ r, icon: '🙋', cls: 'hand', act: 'Assigned to a human (main checkout).' });

  const rank = (it) => (it.r.verdict || '').startsWith('HIGH-IMPACT') ? 0
    : (it.r.verdict || '').startsWith('low-impact') ? 2 : 1;
  items.sort((a, b) => rank(a) - rank(b) || prio(a.r) - prio(b.r));

  let html = items.map((it) => {
    const high = rank(it) === 0, low = rank(it) === 2;
    return `
    <div class="card ${high ? 'high' : low ? 'low' : ''}">
      <div class="card-icon ${it.cls}">${it.icon}</div>
      <div class="card-main">
        <div class="card-title">${esc(it.r.title)}</div>
        <div class="card-sub">${it.act} ${impactSentence(it.r)}</div>
      </div>
      <div class="card-actions">
        ${low ? `<button class="ghost-btn" data-skip="${it.r.id}">Skip for now</button>` : ''}
        <button class="ghost-btn" data-open="${it.r.id}">Details</button>
      </div>
    </div>`;
  }).join('');

  for (const q of (rb.questions || [])) {
    html += `
    <div class="card">
      <div class="card-icon question">❓</div>
      <div class="card-main">
        <div class="card-title">${q.open} open question${q.open === 1 ? '' : 's'} from an agent</div>
        <div class="card-sub">Answer inside <code>${esc(q.file)}</code> — the agent keeps working meanwhile.</div>
      </div>
    </div>`;
  }

  $('#needs-you-cards').innerHTML = html ||
    `<div class="empty-state"><b>Nothing needs you.</b> The team is unblocked — check back later, or browse the map.</div>`;

  document.querySelectorAll('[data-open]').forEach((b) => b.onclick = () => openDrawer(b.dataset.open));
  document.querySelectorAll('[data-skip]').forEach((b) => b.onclick = async () => {
    b.disabled = true;
    await postSet(b.dataset.skip, { deferred: 'yes', reason: 'skipped from dashboard (low impact)' });
  });
}

// ---------------- overview: lane rows ----------------

function renderLaneRows() {
  const active = S.lanes.filter((l) => !(l.lane === 'ml-flywheel' && l.total - l.done === 0) || l.deferred === 0);
  const rows = [...S.lanes].sort((a, b) => b.pct_done - a.pct_done || a.lane.localeCompare(b.lane));
  // paused lanes (everything open is deferred or lane is the parked plan) go last
  const isPausedLane = (l) => l.lane === 'ml-flywheel' || (l.total === l.done && l.deferred > 0);
  rows.sort((a, b) => (isPausedLane(a) ? 1 : 0) - (isPausedLane(b) ? 1 : 0));

  $('#lane-rows').innerHTML = rows.map((l) => {
    const meta = LANE_META[l.lane] || LANE_META['(untagged)'];
    const paused = isPausedLane(l);
    const open = S.tasks.filter((t) => laneOf(t) === l.lane && t.status !== 'done' && !isPaused(t));
    open.sort((a, b) => prio(a) - prio(b));
    const chips = open.slice(0, 5).map((t) => {
      const ds = dispStatus(t), m = STATUS_META[ds];
      return `<span class="chip" data-open="${t.id}" title="${esc(t.title)} — ${m.label}">
        <span class="dot" style="background:${m.color}"></span>${t.id}
        ${t.kind ? `<span class="k k-${t.kind}">${KIND_LABEL[t.kind]}</span>` : ''}
        ${prio(t) <= 2 ? '🔥' : ''}</span>`;
    }).join('');
    const more = open.length > 5 ? `<span class="chip more">+${open.length - 5} more</span>` : '';
    const pct = { done: l.done / l.total * 100 || 0, review: l.review / l.total * 100 || 0, doing: l.doing / l.total * 100 || 0 };
    const a = S.assign[l.lane] || {};
    const assigned = a.owner && a.owner !== 'unassigned';
    const own = assigned
      ? `<span class="own-who">👤 ${esc(a.owner)}</span>`
      : `<span class="own-who unassigned">unassigned</span>`;
    const goal = a.goal ? `<span class="own-goal" title="${esc(a.goal)}">“${esc(a.goal)}”</span>` : '';
    return `
    <div class="lane ${paused ? 'paused' : ''}" data-lane="${l.lane}">
      <div class="lane-top">
        <span class="lane-icon">${meta.icon}</span>
        <span class="lane-name">${esc(meta.name)}</span>
        ${paused ? '<span class="lane-paused-tag">paused</span>' : ''}
        <span class="lane-frac">${l.done} of ${l.total} done${l.deferred ? ` · ${l.deferred} paused` : ''}</span>
        <span class="lane-right">${chips}${more}</span>
      </div>
      <div class="lane-own">
        ${own}
        <span class="own-branch" title="working branch">⑂ ${esc(a.branch || ('lane/' + l.lane))}</span>
        ${goal}
        <button class="assign-btn" data-assign="${l.lane}">${assigned ? 'Reassign' : 'Assign'}</button>
      </div>
      <div class="bar">
        <i class="b-done" style="width:${pct.done}%"></i>
        <i class="b-review" style="width:${pct.review}%"></i>
        <i class="b-doing" style="width:${pct.doing}%"></i>
      </div>
    </div>`;
  }).join('');

  document.querySelectorAll('.lane[data-lane]').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.closest('.chip') || e.target.closest('.lane-own')) return;
      if (el.classList.contains('paused')) return;
      S.laneFilter = el.dataset.lane;
      switchView('map');
    };
  });
  document.querySelectorAll('#lane-rows .chip[data-open]').forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); openDrawer(b.dataset.open); });
  document.querySelectorAll('#lane-rows .assign-btn').forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); openAssign(b.dataset.assign); });
}

// ---------------- map: layout ----------------

const NODE_W = 168, NODE_H = 40, COL_W = 216, ROW_H = 54, BAND_PAD = 26, BAND_GAP = 14;

function visibleTasks() {
  return S.tasks.filter((t) => {
    if (S.hideDone && t.status === 'done') return false;
    if (!S.showPaused && isPaused(t)) return false;
    if (S.laneFilter && laneOf(t) !== S.laneFilter) return false;
    return true;
  });
}

function computeLayout() {
  // depth = longest dependency path (over the FULL graph so arrows flow left->right)
  const depth = {};
  const depthOf = (id, seen = new Set()) => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0; // cycle guard
    seen.add(id);
    const t = S.byId[id];
    const deps = (t && t.depends_on) || [];
    depth[id] = deps.length ? Math.max(...deps.map((d) => depthOf(d, seen))) + 1 : 0;
    return depth[id];
  };
  S.tasks.forEach((t) => depthOf(t.id));

  const vis = visibleTasks();
  const lanesInUse = LANE_ORDER_FALLBACK.filter((l) => vis.some((t) => laneOf(t) === l));
  const pos = {}, bands = [];
  let y = 0;
  for (const lane of lanesInUse) {
    const rows = vis.filter((t) => laneOf(t) === lane)
      .sort((a, b) => depth[a.id] - depth[b.id] || prio(a) - prio(b) || a.id.localeCompare(b.id));
    // greedy row packing: reuse a row when the previous node in it is at a lower depth
    const rowLastDepth = [];
    for (const t of rows) {
      let r = rowLastDepth.findIndex((d) => d < depth[t.id]);
      if (r === -1) { r = rowLastDepth.length; rowLastDepth.push(depth[t.id]); }
      else rowLastDepth[r] = depth[t.id];
      pos[t.id] = { x: depth[t.id] * COL_W, y: y + BAND_PAD + r * ROW_H, t };
    }
    const bandH = BAND_PAD * 2 + Math.max(rowLastDepth.length, 1) * ROW_H - (ROW_H - NODE_H);
    bands.push({ lane, y, h: bandH, count: rows.length });
    y += bandH + BAND_GAP;
  }
  return { pos, bands, totalH: y };
}

// ---------------- map: render ----------------

function renderMap() {
  if (!S.layout) S.layout = computeLayout();
  const { pos, bands } = S.layout;
  const svg = $('#map-svg');
  const q = S.search.toLowerCase();
  const match = (t) => !q || t.id.toLowerCase().includes(q) || (t.title || '').toLowerCase().includes(q);
  const sel = S.selected;
  const selIn = new Set(), selOut = new Set();
  if (sel && S.byId[sel]) {
    (S.byId[sel].depends_on || []).forEach((d) => selIn.add(d));
    (S.dependents[sel] || []).forEach((d) => selOut.add(d));
  }

  const maxX = Math.max(0, ...Object.values(pos).map((p) => p.x)) + NODE_W + 120;
  let g = `<g id="map-g" transform="translate(${S.tx},${S.ty}) scale(${S.scale})">`;

  bands.forEach((b, i) => {
    g += `<rect class="band ${i % 2 ? 'alt' : ''}" x="${-60}" y="${b.y}" width="${maxX + 120}" height="${b.h}" rx="12"></rect>`;
  });

  // edges under nodes
  for (const [id, p] of Object.entries(pos)) {
    for (const d of (p.t.depends_on || [])) {
      const s = pos[d];
      if (!s) continue;
      const x1 = s.x + NODE_W, y1 = s.y + NODE_H / 2, x2 = p.x, y2 = p.y + NODE_H / 2;
      const mx = (x1 + x2) / 2;
      let cls = 'edge';
      if (sel) cls += id === sel ? ' hl-in' : d === sel ? ' hl-out' : ' dim';
      g += `<path class="${cls}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"></path>`;
    }
  }

  for (const [id, p] of Object.entries(pos)) {
    const ds = dispStatus(p.t), m = STATUS_META[ds];
    const dim = (q && !match(p.t)) || (sel && id !== sel && !selIn.has(id) && !selOut.has(id) && q === '');
    const title = (p.t.title || '').slice(0, 26) + ((p.t.title || '').length > 26 ? '…' : '');
    g += `<g class="node ${dim && q ? 'dim' : ''} ${sel === id ? 'selected' : ''}" data-id="${id}" transform="translate(${p.x},${p.y})">
      <rect width="${NODE_W}" height="${NODE_H}" fill="${m.soft}" stroke="${m.color}" ${ds === 'backlog' || ds === 'paused' ? 'stroke-dasharray="4 3"' : ''}></rect>
      <text class="n-id" x="10" y="16" fill="${m.color}">${id}${prio(p.t) <= 2 ? ' 🔥' : ''}${p.t.kind === 'fix' ? ' <tspan class="n-flag" fill="var(--fix)">FIX</tspan>' : ''}</text>
      <text class="n-title" x="10" y="31" fill="var(--text-dim)">${esc(title)}</text>
    </g>`;
  }
  g += '</g>';

  // pinned lane labels (x fixed in screen space, y follows the transform)
  let labels = '<g>';
  bands.forEach((b) => {
    const sy = b.y * S.scale + S.ty;
    const meta = LANE_META[b.lane] || LANE_META['(untagged)'];
    const a = S.assign[b.lane] || {};
    const owner = (a.owner && a.owner !== 'unassigned')
      ? ` <tspan class="owner">· 👤 ${esc(a.owner)}</tspan>`
      : '';
    const branch = a.branch ? ` <tspan class="branch">· ⑂ ${esc(a.branch)}</tspan>` : '';
    labels += `<text class="band-label" x="14" y="${sy + 19}">${meta.icon} ${esc(meta.name)} <tspan class="cnt">· ${b.count}</tspan>${owner}${branch}</text>`;
  });
  labels += '</g>';

  svg.innerHTML = g + labels;
  svg.querySelectorAll('.node').forEach((n) => {
    n.addEventListener('click', (e) => { e.stopPropagation(); S.selected = n.dataset.id; renderMap(); openDrawer(n.dataset.id); });
  });

  // lane filter chip
  $('#map-lane-filter').innerHTML = S.laneFilter
    ? `<span class="chip" id="clear-lane-filter">Showing: ${esc(laneName(S.laneFilter))} ✕</span>` : '';
  const clear = $('#clear-lane-filter');
  if (clear) clear.onclick = () => { S.laneFilter = null; S.layout = null; fitMap(); };

  renderLegend();
}

function renderLegend() {
  $('#legend').innerHTML = ['ready', 'waiting', 'doing', 'review', 'blocked', 'done', 'backlog']
    .map((k) => `<span><i class="sw" style="background:${STATUS_META[k].color}"></i>${STATUS_META[k].label}</span>`)
    .join('');
}

function fitMap() {
  if (!S.layout) S.layout = computeLayout();
  const ps = Object.values(S.layout.pos);
  const wrap = $('#map-wrap');
  if (!ps.length || !wrap.clientWidth) { renderMap(); return; }
  const minX = Math.min(...ps.map((p) => p.x)) - 40;
  const maxX = Math.max(...ps.map((p) => p.x)) + NODE_W + 40;
  const maxY = S.layout.totalH + 30;
  S.scale = Math.min(wrap.clientWidth / (maxX - minX + 200), wrap.clientHeight / maxY, 1.35);
  S.scale = Math.max(S.scale, 0.12);
  S.tx = 255 - minX * S.scale;  // leave room for pinned lane labels
  S.ty = 12;
  renderMap();
}

// ---------------- map: pan/zoom ----------------

function bindMap() {
  const wrap = $('#map-wrap');
  let dragging = false, sx = 0, sy = 0, ox = 0, oy = 0, moved = false;
  wrap.addEventListener('mousedown', (e) => {
    dragging = true; moved = false; sx = e.clientX; sy = e.clientY; ox = S.tx; oy = S.ty;
    wrap.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) > 3) moved = true;
    S.tx = ox + e.clientX - sx; S.ty = oy + e.clientY - sy;
    renderMap();
  });
  window.addEventListener('mouseup', () => { dragging = false; wrap.classList.remove('dragging'); });
  wrap.addEventListener('click', () => { if (!moved && S.selected) { S.selected = null; renderMap(); } });
  wrap.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const f = e.deltaY < 0 ? 1.13 : 1 / 1.13;
    const ns = Math.min(Math.max(S.scale * f, 0.1), 3);
    S.tx = mx - (mx - S.tx) * (ns / S.scale);
    S.ty = my - (my - S.ty) * (ns / S.scale);
    S.scale = ns;
    renderMap();
  }, { passive: false });

  $('#zoom-in').onclick = () => { S.scale = Math.min(S.scale * 1.25, 3); renderMap(); };
  $('#zoom-out').onclick = () => { S.scale = Math.max(S.scale / 1.25, 0.1); renderMap(); };
  $('#zoom-fit').onclick = fitMap;
  $('#map-search').addEventListener('input', (e) => { S.search = e.target.value; renderMap(); });
  $('#t-hide-done').addEventListener('change', (e) => { S.hideDone = e.target.checked; S.layout = null; fitMap(); });
  $('#t-show-paused').addEventListener('change', (e) => { S.showPaused = e.target.checked; S.layout = null; fitMap(); });
}

// ---------------- drawer ----------------

function statusSentence(t) {
  const ds = dispStatus(t), m = STATUS_META[ds];
  const dot = `<span class="dot" style="background:${m.color}"></span>`;
  switch (ds) {
    case 'doing': return `${dot}<b>${m.label}</b> — ${esc(t.owner)} is on it.`;
    case 'review': return `${dot}<b>${m.label}</b> — ${esc(t.owner)} finished; a human needs to review & merge branch <code>${esc(t.branch)}</code>.`;
    case 'blocked': return `${dot}<b>${m.label}</b> — needs a human decision to continue.`;
    case 'ready': return `${dot}<b>${m.label}</b> — an agent can pick this up any time.`;
    case 'waiting': {
      const open = (t.depends_on || []).filter((d) => (S.byId[d] || {}).status !== 'done');
      return `${dot}<b>${m.label}</b> — waiting for ${open.map((d) => `<code>${d}</code>`).join(', ') || 'dependencies'}.`;
    }
    case 'done': return `${dot}<b>${m.label}</b> — merged.`;
    case 'paused': return `${dot}<b>${m.label}</b> — deliberately skipped for now.`;
    default: return `${dot}<b>${m.label}</b>.`;
  }
}

function fillDrawerHead(t) {
  if (!t) return;
  $('#drawer-id').textContent = t.id;
  $('#drawer-title').textContent = t.title;
  $('#drawer-statusline').innerHTML = statusSentence(t);
  const meta = LANE_META[laneOf(t)];
  $('#drawer-badges').innerHTML = [
    `<span class="badge">${meta.icon} ${esc(meta.name)}</span>`,
    t.kind ? `<span class="badge k-${t.kind}">${KIND_LABEL[t.kind]}</span>` : '',
    `<span class="badge">priority ${prio(t)}${prio(t) <= 2 ? ' 🔥' : ''}</span>`,
    t.owner && t.owner !== 'unassigned' ? `<span class="badge">👤 ${esc(t.owner)}</span>` : '',
  ].filter(Boolean).join('');

  const deps = (t.depends_on || []);
  const dependents = S.dependents[t.id] || [];
  const chip = (id) => {
    const dt = S.byId[id]; if (!dt) return '';
    const m = STATUS_META[dispStatus(dt)];
    return `<span class="chip" data-open="${id}"><span class="dot" style="background:${m.color}"></span>${id}</span>`;
  };
  $('#drawer-deps').innerHTML =
    (deps.length ? `<h4>Waits for</h4><div class="dep-chips">${deps.map(chip).join('')}</div>` : '') +
    (dependents.length ? `<h4 style="margin-top:${deps.length ? 14 : 0}px">Unlocks</h4><div class="dep-chips">${dependents.map(chip).join('')}</div>` : '') ||
    '<h4>Dependencies</h4><span class="muted">None — independent task.</span>';
  $('#drawer-deps').querySelectorAll('.chip[data-open]').forEach((b) => b.onclick = () => openDrawer(b.dataset.open));

  // steering current values
  S.steerPriority = prio(t);
  renderPrioritySeg();
  $('#steer-lane').value = laneOf(t);
  $('#steer-kind').value = t.kind || 'new';
  $('#steer-deferred').checked = isPaused(t);
}

async function openDrawer(id) {
  const t = S.byId[id];
  if (!t) return;
  S.drawerId = id;
  $('#drawer').classList.remove('hidden');
  $('#drawer-scrim').classList.remove('hidden');
  fillDrawerHead(t);
  $('#drawer-dod').innerHTML = '<li class="muted">loading…</li>';
  $('#drawer-log').innerHTML = '<li class="muted">loading…</li>';
  try {
    const d = await fetch(`/api/task/${id}.json`).then((r) => r.json());
    const norm = (x) => typeof x === 'string'
      ? { checked: /\[[xX]\]/.test(x), text: x.replace(/^\s*-\s*\[[ xX]\]\s*/, '') }
      : { checked: !!(x.checked ?? x.done), text: x.text ?? x.label ?? JSON.stringify(x) };
    const dod = (d.dod || []).map(norm);
    $('#drawer-dod').innerHTML = dod.length
      ? dod.map((i) => `<li><span class="tick ${i.checked ? 'on' : 'off'}">${i.checked ? '✓' : '○'}</span><span>${esc(i.text)}</span></li>`).join('')
      : '<li class="muted">No checklist on this task.</li>';
    const log = (d.log || []);
    $('#drawer-log').innerHTML = log.length
      ? log.map((l) => `<li>${esc(typeof l === 'string' ? l : JSON.stringify(l)).replace(/^-\s*/, '')}</li>`).join('')
      : '<li class="muted">No activity notes yet.</li>';
  } catch {
    $('#drawer-dod').innerHTML = '<li class="muted">couldn’t load details</li>';
  }
}

function closeDrawer() {
  S.drawerId = null;
  $('#drawer').classList.add('hidden');
  $('#drawer-scrim').classList.add('hidden');
}

function renderPrioritySeg() {
  const labels = { 1: '1 · critical', 2: '2', 3: '3', 4: '4', 5: '5 · someday' };
  $('#steer-priority').innerHTML = [1, 2, 3, 4, 5].map((p) =>
    `<button class="${S.steerPriority === p ? 'on' : ''}" data-p="${p}">${labels[p]}</button>`).join('');
  $('#steer-priority').querySelectorAll('button').forEach((b) =>
    b.onclick = () => { S.steerPriority = +b.dataset.p; renderPrioritySeg(); });
}

async function postSet(id, fields) {
  try {
    const res = await fetch('/api/set', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, ...fields }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) { toast('Saved ✓ — the board updated', 'ok'); await fetchAll(true); }
    else toast(body.error || body.detail || `Couldn't save (${res.status}) — try again in a minute`, 'err');
  } catch { toast('Couldn’t reach the server', 'err'); }
}

// ---------------- codebase node graph (canvas) ----------------

const G_CELL = 80;  // world-unit spatial-hash cell for hover picking

function commColor(c) {
  if (c < 0) return '#5a6577';
  const h = (c * 137.508) % 360;
  return `hsl(${h.toFixed(0)}, 62%, 62%)`;
}

async function loadGraph() {
  if (S.gLoaded) { drawGraph(); return; }
  if (S.gLoading) return;
  S.gLoading = true;
  $('#graph-stats').textContent = 'loading graph…';
  try {
    const g = await fetch('/api/graph.json').then((r) => r.json());
    if (g.error || !g.nodes || !g.nodes.length) {
      const em = $('#graph-empty');
      em.textContent = g.error || 'No graph yet — run: graphify update .';
      em.classList.remove('hidden');
      $('#graph-stats').textContent = 'no graph';
      S.gLoading = false; return;
    }
    S.graph = g;
    g.lc = g.nodes.map((n) => (n[3] || '').toLowerCase());
    const grid = new Map(), byComm = new Map();
    for (let i = 0; i < g.nodes.length; i++) {
      const n = g.nodes[i];
      const key = Math.floor(n[0] / G_CELL) + ',' + Math.floor(n[1] / G_CELL);
      (grid.get(key) || grid.set(key, []).get(key)).push(i);
      (byComm.get(n[2]) || byComm.set(n[2], []).get(n[2])).push(i);
    }
    S.gGrid = grid; S.gCellByComm = byComm;
    const s = g.stats;
    $('#graph-stats').innerHTML =
      `<b>${s.nodes.toLocaleString()}</b> nodes · <b>${s.edges.toLocaleString()}</b> edges · ${s.communities.toLocaleString()} communities`;
    S.gLoaded = true; S.gLoading = false;
    fitGraph();
  } catch (e) {
    $('#graph-stats').textContent = 'graph failed to load';
    S.gLoading = false;
  }
}

function resizeGraphCanvas() {
  const wrap = $('#graph-wrap'), cv = $('#graph-canvas');
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.width = w + 'px'; cv.style.height = h + 'px';
  }
  return { w, h, dpr };
}

function fitGraph() {
  if (!S.graph) return;
  const wrap = $('#graph-wrap');
  const w = wrap.clientWidth || 800, h = wrap.clientHeight || 600;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of S.graph.nodes) {
    if (n[0] < minX) minX = n[0]; if (n[0] > maxX) maxX = n[0];
    if (n[1] < minY) minY = n[1]; if (n[1] > maxY) maxY = n[1];
  }
  const gw = (maxX - minX) || 1, gh = (maxY - minY) || 1;
  S.gscale = Math.min(w / (gw * 1.08), h / (gh * 1.08));
  S.gx = w / 2 - (minX + maxX) / 2 * S.gscale;
  S.gy = h / 2 - (minY + maxY) / 2 * S.gscale;
  drawGraph();
}

function renderEdges(ctx, w, h) {
  const nodes = S.graph.nodes, edges = S.graph.edges, sc = S.gscale, ox = S.gx, oy = S.gy;
  ctx.strokeStyle = 'rgba(150,170,205,0.05)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let k = 0; k < edges.length; k++) {
    const a = nodes[edges[k][0]], b = nodes[edges[k][1]];
    const ax = a[0] * sc + ox, ay = a[1] * sc + oy, bx = b[0] * sc + ox, by = b[1] * sc + oy;
    if ((ax < 0 && bx < 0) || (ax > w && bx > w) || (ay < 0 && by < 0) || (ay > h && by > h)) continue;
    ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
  }
  ctx.stroke();
}

function renderNodes(ctx, w, h) {
  const nodes = S.graph.nodes, sc = S.gscale, ox = S.gx, oy = S.gy, q = S.gSearch, r = q ? 1.7 : 2.0;
  for (const [comm, list] of S.gCellByComm) {
    ctx.fillStyle = commColor(comm);
    for (let j = 0; j < list.length; j++) {
      const i = list[j], n = nodes[i];
      const x = n[0] * sc + ox, y = n[1] * sc + oy;
      if (x < -4 || x > w + 4 || y < -4 || y > h + 4) continue;
      ctx.globalAlpha = q ? (S.graph.lc[i].includes(q) ? 1 : 0.1) : 1;
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
    }
  }
  ctx.globalAlpha = 1;
}

// Full-quality edges+nodes are expensive (73k segments), so we render them once
// into an offscreen canvas per settled transform; hover just blits it + a ring.
function renderBase() {
  const { w, h, dpr } = resizeGraphCanvas();
  const off = S.gBase || (S.gBase = document.createElement('canvas'));
  if (off.width !== Math.round(w * dpr) || off.height !== Math.round(h * dpr)) {
    off.width = Math.round(w * dpr); off.height = Math.round(h * dpr);
  }
  const ctx = off.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (S.gEdges) renderEdges(ctx, w, h);
  renderNodes(ctx, w, h);
  S.gBaseValid = true;
  present();
}

function present() {
  const cv = $('#graph-canvas');
  const ctx = cv.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (S.gBase && S.gBaseValid) ctx.drawImage(S.gBase, 0, 0);
  if (S.gHover >= 0 && S.graph) {
    const dpr = window.devicePixelRatio || 1, n = S.graph.nodes[S.gHover];
    const x = (n[0] * S.gscale + S.gx) * dpr, y = (n[1] * S.gscale + S.gy) * dpr;
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath(); ctx.arc(x, y, 6 * dpr, 0, Math.PI * 2); ctx.stroke();
  }
}

let _gEdgeTimer = null;
function drawGraph(fast) {
  if (!S.graph) return;
  if (fast) {
    // pan/zoom in progress: nodes-only, direct to the visible canvas
    const { w, h, dpr } = resizeGraphCanvas();
    const ctx = $('#graph-canvas').getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    renderNodes(ctx, w, h);
    S.gBaseValid = false;
    return;
  }
  renderBase();
}

function graphEvtPos(e) {
  const rect = $('#graph-wrap').getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  return { mx, my, wx: (mx - S.gx) / S.gscale, wy: (my - S.gy) / S.gscale };
}

function nearestNode(wx, wy, pxThresh) {
  if (!S.gGrid) return -1;
  const thr = pxThresh / S.gscale;
  const span = Math.max(1, Math.ceil(thr / G_CELL));
  const cx = Math.floor(wx / G_CELL), cy = Math.floor(wy / G_CELL);
  let best = -1, bestD = thr * thr;
  for (let gx = cx - span; gx <= cx + span; gx++)
    for (let gy = cy - span; gy <= cy + span; gy++) {
      const list = S.gGrid.get(gx + ',' + gy);
      if (!list) continue;
      for (const i of list) {
        const n = S.graph.nodes[i], dx = n[0] - wx, dy = n[1] - wy, d = dx * dx + dy * dy;
        if (d < bestD) { bestD = d; best = i; }
      }
    }
  return best;
}

function bindGraph() {
  const wrap = $('#graph-wrap');
  let dragging = false, sxp = 0, syp = 0, ox0 = 0, oy0 = 0;
  wrap.addEventListener('mousedown', (e) => {
    dragging = true; sxp = e.clientX; syp = e.clientY; ox0 = S.gx; oy0 = S.gy;
    wrap.classList.add('dragging'); $('#graph-tip').classList.add('hidden');
  });
  window.addEventListener('mousemove', (e) => {
    if (dragging) {
      S.gx = ox0 + e.clientX - sxp; S.gy = oy0 + e.clientY - syp;
      drawGraph(true); return;
    }
    if (S.view !== 'codebase' || !S.gLoaded) return;
    if (wrap._hoverRAF) return;
    wrap._hoverRAF = requestAnimationFrame(() => {
      wrap._hoverRAF = 0;
      const { mx, my, wx, wy } = graphEvtPos(e);
      const i = nearestNode(wx, wy, 9);
      const tip = $('#graph-tip');
      if (i !== S.gHover) { S.gHover = i; present(); }
      if (i >= 0) {
        const n = S.graph.nodes[i];
        tip.innerHTML = `<b>${esc(n[3])}</b>${n[4] ? `<span>${esc(n[4])}</span>` : ''}`;
        tip.style.left = (mx + 14) + 'px'; tip.style.top = (my + 12) + 'px';
        tip.classList.remove('hidden');
      } else tip.classList.add('hidden');
    });
  });
  window.addEventListener('mouseup', () => {
    if (dragging) { dragging = false; wrap.classList.remove('dragging'); drawGraph(false); }
  });
  wrap.addEventListener('wheel', (e) => {
    e.preventDefault();
    const { mx, my } = graphEvtPos(e);
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const ns = Math.min(Math.max(S.gscale * f, 0.02), 40);
    S.gx = mx - (mx - S.gx) * (ns / S.gscale);
    S.gy = my - (my - S.gy) * (ns / S.gscale);
    S.gscale = ns;
    drawGraph(true);
    clearTimeout(_gEdgeTimer); _gEdgeTimer = setTimeout(() => drawGraph(false), 130);
  }, { passive: false });
  wrap.addEventListener('mouseleave', () => $('#graph-tip').classList.add('hidden'));

  $('#graph-fit').onclick = fitGraph;
  $('#graph-edges').addEventListener('change', (e) => { S.gEdges = e.target.checked; drawGraph(); });
  $('#graph-search').addEventListener('input', (e) => { S.gSearch = e.target.value.trim().toLowerCase(); drawGraph(); });
  $('#graph-ask-toggle').onclick = () => {
    const el = $('#graph-ask'); el.classList.toggle('hidden');
    if (!el.classList.contains('hidden')) { loadGraphifyFreshness(); $('#graphify-q').focus(); }
  };
}

// ---------------- codebase (graphify) query ----------------

let graphifyFreshLoaded = false;

function bindGraphify() {
  const go = async () => {
    const q = $('#graphify-q').value.trim();
    if (!q) return;
    $('#graphify-out').textContent = 'Thinking…';
    try {
      const r = await fetch(`/api/graphify.json?mode=${$('#graphify-mode').value}&q=${encodeURIComponent(q)}`)
        .then((r) => r.json());
      $('#graphify-out').textContent = r.result || r.error || 'No result.';
    } catch { $('#graphify-out').textContent = 'Query failed.'; }
  };
  $('#graphify-go').onclick = go;
  $('#graphify-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

async function loadGraphifyFreshness() {
  if (graphifyFreshLoaded) return;
  graphifyFreshLoaded = true;
  try {
    const f = await fetch('/api/freshness.json').then((r) => r.json());
    $('#graphify-freshness').textContent = f.note || '';
  } catch { /* non-fatal */ }
}

// ---------------- assign-lane modal ----------------

function openAssign(lane) {
  S.assignLane = lane;
  const a = S.assign[lane] || {};
  const meta = LANE_META[lane] || LANE_META['(untagged)'];
  $('#assign-title').innerHTML = `Assign — ${meta.icon} ${esc(meta.name)}`;
  $('#assign-owner').value = (a.owner && a.owner !== 'unassigned') ? a.owner : '';
  $('#assign-branch').value = a.branch || (lane === '(untagged)' ? 'lane/misc-fixes' : `lane/${lane}`);
  $('#assign-goal').value = a.goal || '';
  $('#assign-error').classList.add('hidden');
  $('#assign-modal').classList.remove('hidden');
  $('#assign-owner').focus();
}

function closeAssign() {
  S.assignLane = null;
  $('#assign-modal').classList.add('hidden');
}

function assignError(msg) {
  const el = $('#assign-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

async function submitAssign(clear) {
  if (!S.assignLane) return;
  const payload = { lane: S.assignLane };
  if (clear) {
    payload.clear = true;
  } else {
    const owner = $('#assign-owner').value.trim();
    if (!owner) { assignError('Enter an owner (or use Unassign).'); return; }
    payload.owner = owner;
    payload.goal = $('#assign-goal').value.trim();
  }
  const branch = $('#assign-branch').value.trim();
  if (branch) {
    if (!/^lane\/[a-z0-9._-]+$/.test(branch)) {
      assignError('Branch must look like lane/… (lowercase, no spaces).');
      return;
    }
    payload.branch = branch;
  }
  $('#assign-save').disabled = true;
  $('#assign-clear').disabled = true;
  try {
    const res = await fetch('/api/assign', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) {
      toast(clear ? 'Lane unassigned ✓' : 'Lane assigned ✓ — synced to the team', 'ok');
      closeAssign();
      await fetchAll(true);
    } else if (res.status === 409) {
      assignError('Board is busy syncing — try again in a minute.');
    } else {
      assignError(body.out || body.error || `Couldn't save (${res.status}).`);
    }
  } catch {
    assignError('Couldn’t reach the server.');
  } finally {
    $('#assign-save').disabled = false;
    $('#assign-clear').disabled = false;
  }
}

function bindAssign() {
  $('#assign-close').onclick = closeAssign;
  $('#assign-save').onclick = () => submitAssign(false);
  $('#assign-clear').onclick = () => submitAssign(true);
  $('#assign-modal').addEventListener('click', (e) => {
    if (e.target.id === 'assign-modal') closeAssign();
  });
}

// ---------------- shell ----------------

function switchView(v) {
  S.view = v;
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $('#view-overview').classList.toggle('hidden', v !== 'overview');
  $('#view-map').classList.toggle('hidden', v !== 'map');
  $('#view-codebase').classList.toggle('hidden', v !== 'codebase');
  if (v === 'map') { S.layout = null; fitMap(); }
  if (v === 'codebase') { requestAnimationFrame(() => loadGraph()); }
}

function toast(msg, cls) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast ${cls || ''}`;
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 4200);
}

function tickRefreshNote() {
  const s = Math.round((Date.now() - S.lastFetch) / 1000);
  $('#refresh-note').textContent = S.lastFetch ? `updated ${s < 5 ? 'just now' : s + 's ago'}` : 'loading…';
}

function init() {
  document.querySelectorAll('.nav-btn').forEach((b) => b.onclick = () => switchView(b.dataset.view));
  $('#drawer-close').onclick = closeDrawer;
  $('#drawer-scrim').onclick = closeDrawer;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeDrawer(); closeAssign(); }
  });
  $('#steer-lane').innerHTML = Object.entries(LANE_META)
    .filter(([k]) => k !== '(untagged)')
    .map(([k, v]) => `<option value="${k}">${v.icon} ${v.name}</option>`).join('');
  $('#steer-apply').onclick = async () => {
    if (!S.drawerId) return;
    const t = S.byId[S.drawerId];
    const fields = {};
    if (S.steerPriority !== prio(t)) fields.priority = S.steerPriority;
    if ($('#steer-lane').value !== laneOf(t)) fields.lane = $('#steer-lane').value;
    if ($('#steer-kind').value !== (t.kind || 'new')) fields.kind = $('#steer-kind').value;
    if ($('#steer-deferred').checked !== isPaused(t)) fields.deferred = $('#steer-deferred').checked ? 'yes' : 'no';
    const reason = $('#steer-reason').value.trim();
    if (reason) fields.reason = reason;
    if (!Object.keys(fields).length || (Object.keys(fields).length === 1 && fields.reason)) {
      toast('Nothing changed'); return;
    }
    $('#steer-apply').disabled = true;
    await postSet(S.drawerId, fields);
    $('#steer-apply').disabled = false;
    $('#steer-reason').value = '';
  };
  bindMap();
  bindGraph();
  bindGraphify();
  bindAssign();
  fetchAll(true);
  setInterval(() => fetchAll(false), 30000);
  setInterval(tickRefreshNote, 5000);
  window.addEventListener('resize', () => {
    if (S.view === 'map') renderMap();
    if (S.view === 'codebase' && S.gLoaded) drawGraph();
  });
}

document.addEventListener('DOMContentLoaded', init);
