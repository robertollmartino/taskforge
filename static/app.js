/**
 * TaskForge Frontend Core (Mobile-First, Zero-Flicker SSE Singleton, Table/Kanban/List, Advanced Filters, PWA Push)
 */
const state = {
  token: localStorage.getItem('tf_token') || '',
  user: null,
  tasks: [],
  totalTasks: 0,
  page: 1,
  pageSize: 100,
  totalPages: 1,
  groups: null,
  projects: [],
  statuses: [],
  customFields: [],
  users: [],
  views: [],
  activeView: null,
  viewMode: 'table', // 'table' | 'kanban' | 'list'
  selectedProjects: [], // multi-project filter
  searchQuery: '',
  filterTree: { logic: 'AND', conditions: [], groups: [] },
  sorts: [{ field: 'priority', dir: 'asc' }, { field: 'due_date', dir: 'asc' }],
  groupBy: '',
  visibleColumns: ['code', 'title', 'status_slug', 'priority', 'project_id', 'assignee_id', 'due_date', 'tags'],
  columnOrder: ['code', 'title', 'status_slug', 'priority', 'project_id', 'assignee_id', 'due_date', 'tags'],
  activeTaskModalId: null,
  sseSource: null,
  sseThrottleTimer: null,
  deferredPwaPrompt: null,
  activeAdminTab: 'fields'
};

const NATIVE_COLUMNS = [
  { key: 'code', label: 'Código' },
  { key: 'title', label: 'Título' },
  { key: 'status_slug', label: 'Status' },
  { key: 'priority', label: 'Prioridade' },
  { key: 'project_id', label: 'Projeto' },
  { key: 'assignee_id', label: 'Responsável' },
  { key: 'due_date', label: 'Vencimento' },
  { key: 'tags', label: 'Etiquetas' },
  { key: 'subtasks', label: 'Checklist' }
];

// =====================================================================
// UTILITIES & API CLIENT
// =====================================================================
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  const colors = {
    info: 'from-orange-500/90 via-pink-500/90 to-purple-600/90 text-white',
    success: 'from-emerald-500/95 to-teal-600/95 text-white',
    error: 'from-rose-600/95 to-pink-600/95 text-white'
  };
  el.className = `pointer-events-auto px-4 py-2.5 rounded-2xl bg-gradient-to-r ${colors[type] || colors.info} text-xs font-bold shadow-xl backdrop-blur-md transition-all duration-300 transform translate-y-0`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (!(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  if (state.token) {
    headers['Authorization'] = `Bearer ${state.token}`;
  }
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    handleLogout();
    throw new Error('Sessão expirada ou não autenticada.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Erro HTTP ${res.status}`);
  }
  return data;
}

// =====================================================================
// THEME & PWA SERVICE WORKER INITIALIZATION
// =====================================================================
function initTheme() {
  const saved = localStorage.getItem('tf_theme') || 'dark';
  document.documentElement.classList.toggle('dark', saved === 'dark');
}

function toggleTheme() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('tf_theme', isDark ? 'dark' : 'light');
}

async function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    try {
      await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    } catch (e) {
      console.warn('Service Worker error:', e);
    }
  }
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  state.deferredPwaPrompt = e;
  const btn = document.getElementById('btn-install-pwa');
  if (btn) btn.classList.remove('hidden');
});

// =====================================================================
// SINGLETON SSE CONNECTION (Zero-Flicker & 350ms Throttling)
// =====================================================================
function connectSingletonSSE() {
  if (state.sseSource) {
    state.sseSource.close();
    state.sseSource = null;
  }
  if (!state.token) return;

  const es = new EventSource(`/api/v1/stream?token=${encodeURIComponent(state.token)}`);
  state.sseSource = es;

  es.onopen = () => {
    const ind = document.getElementById('sse-indicator');
    const lbl = document.getElementById('sse-label');
    if (ind) ind.className = 'w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
    if (lbl) lbl.textContent = 'Live Stream';
  };

  es.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'connected') return;
      scheduleThrottledUIUpdate(msg);
    } catch (e) {}
  };

  es.onerror = () => {
    const ind = document.getElementById('sse-indicator');
    const lbl = document.getElementById('sse-label');
    if (ind) ind.className = 'w-2 h-2 rounded-full bg-amber-500';
    if (lbl) lbl.textContent = 'Reconectando';
  };
}

function scheduleThrottledUIUpdate(msg) {
  if (state.sseThrottleTimer) return;
  state.sseThrottleTimer = setTimeout(async () => {
    state.sseThrottleTimer = null;
    if (msg.type && msg.type.startsWith('task.')) {
      await fetchTasks();
      if (state.activeTaskModalId && msg.delta && msg.delta.id === state.activeTaskModalId) {
        openTaskModal(state.activeTaskModalId, false);
      }
    } else if (msg.type && (msg.type.startsWith('project.') || msg.type.startsWith('status.') || msg.type.startsWith('custom_field.') || msg.type.startsWith('view.'))) {
      await loadMetadata();
      await fetchTasks();
    } else if (msg.type === 'notification.created') {
      await refreshNotificationBadge();
      if (msg.delta && msg.delta.title) {
        showToast(`${msg.delta.title}`, 'info');
      }
    }
  }, 350);
}

// =====================================================================
// AUTHENTICATION & BOOTSTRAP
// =====================================================================
function handleLogout() {
  state.token = '';
  state.user = null;
  localStorage.removeItem('tf_token');
  if (state.sseSource) {
    state.sseSource.close();
    state.sseSource = null;
  }
  document.getElementById('login-screen').classList.remove('hidden');
}

async function bootstrapApp() {
  initTheme();
  registerServiceWorker();

  if (!state.token) {
    document.getElementById('login-screen').classList.remove('hidden');
    return;
  }

  try {
    const me = await api('/api/v1/auth/me');
    state.user = me.user;
    document.getElementById('login-screen').classList.add('hidden');
    renderUserHeader();
    await loadMetadata();
    selectInitialView();
    await fetchTasks();
    await refreshNotificationBadge();
    connectSingletonSSE();
  } catch (e) {
    handleLogout();
  }
}

function renderUserHeader() {
  if (!state.user) return;
  document.getElementById('user-display-name').textContent = state.user.full_name || state.user.username;
  document.getElementById('user-display-role').textContent = state.user.role;
  document.getElementById('user-avatar').textContent = (state.user.full_name || state.user.username || 'U')[0].toUpperCase();
}

async function loadMetadata() {
  const [projRes, statRes, cfRes, viewsRes, usersRes] = await Promise.all([
    api('/api/v1/projects'),
    api('/api/v1/statuses'),
    api('/api/v1/custom-fields'),
    api('/api/v1/views'),
    api('/api/v1/users')
  ]);
  state.projects = projRes.items || [];
  state.statuses = statRes.items || [];
  state.customFields = cfRes.items || [];
  state.views = viewsRes.items || [];
  state.users = usersRes.items || [];

  renderSidebarViews();
  renderSidebarProjects();
  populateQuickAddDropdowns();
  renderColumnsSelector();
  populateGroupByDropdown();
}

function selectInitialView() {
  if (state.activeView) return;
  const def = state.views.find((v) => v.is_default) || state.views[0];
  if (def) {
    applySavedView(def, false);
  }
}

function applySavedView(view, shouldFetch = true) {
  state.activeView = view;
  state.viewMode = view.view_type || 'table';
  state.filterTree = JSON.parse(JSON.stringify(view.filters || { logic: 'AND', conditions: [], groups: [] }));
  state.sorts = JSON.parse(JSON.stringify(view.sorts && view.sorts.length ? view.sorts : [{ field: 'priority', dir: 'asc' }]));
  state.groupBy = view.group_by || '';

  const allKeys = getAllAvailableColumns().map((c) => c.key);
  state.visibleColumns = (view.visible_columns && view.visible_columns.length)
    ? view.visible_columns.slice()
    : allKeys.slice(0, 8);
  state.columnOrder = (view.column_order && view.column_order.length)
    ? view.column_order.slice()
    : state.visibleColumns.slice();

  // Ensure any custom field keys are also present in columnOrder
  for (const k of allKeys) {
    if (!state.columnOrder.includes(k)) state.columnOrder.push(k);
  }

  document.getElementById('active-view-title').textContent = view.name;
  updateViewModeButtons();
  renderSidebarViews();
  renderFilterBuilder();
  renderColumnsSelector();

  const grpEl = document.getElementById('view-group-by');
  if (grpEl) grpEl.value = state.groupBy;
  if (state.sorts[0]) {
    const sf = document.getElementById('view-sort-field');
    const sd = document.getElementById('view-sort-dir');
    if (sf) sf.value = state.sorts[0].field;
    if (sd) sd.value = state.sorts[0].dir;
  }

  if (shouldFetch) {
    state.page = 1;
    fetchTasks();
  }
}

// =====================================================================
// SIDEBAR & NAVIGATION RENDERING
// =====================================================================
function renderSidebarViews() {
  const container = document.getElementById('sidebar-views-list');
  if (!container) return;
  const icons = { table: '⊞', kanban: ' Columns ', list: '☰' };
  const iconEmoji = { table: '📊', kanban: '📋', list: '📑' };

  container.innerHTML = state.views.map((v) => {
    const active = state.activeView && state.activeView.id === v.id;
    return `
      <div class="group flex items-center justify-between rounded-xl px-3 py-2 cursor-pointer transition-all ${
        active
          ? 'bg-gradient-to-r from-orange-500/15 via-pink-500/15 to-purple-500/15 text-pink-500 font-bold border border-pink-500/30'
          : 'text-zinc-600 dark:text-zinc-300 hover:bg-zinc-200/60 dark:hover:bg-zinc-800/60'
      }" onclick="handleSelectView('${v.id}')">
        <div class="flex items-center gap-2 truncate">
          <span>${iconEmoji[v.view_type] || '📊'}</span>
          <span class="text-xs truncate">${escapeHtml(v.name)}</span>
        </div>
        <div class="flex items-center gap-1">
          ${v.is_default ? '<span class="text-[9px] px-1.5 py-0.2 rounded bg-orange-500/20 text-orange-400">Padrão</span>' : ''}
          ${v.is_shared ? '<span class="text-[9px] text-zinc-400" title="Compartilhada">🌐</span>' : '<span class="text-[9px] text-zinc-400" title="Privada">🔒</span>'}
        </div>
      </div>
    `;
  }).join('');
}

window.handleSelectView = function(viewId) {
  const v = state.views.find((x) => x.id === viewId);
  if (v) {
    applySavedView(v, true);
    closeMobileSidebar();
  }
};

function renderSidebarProjects() {
  const container = document.getElementById('sidebar-projects-list');
  const clearBtn = document.getElementById('btn-clear-projects');
  if (!container) return;

  if (clearBtn) {
    clearBtn.classList.toggle('hidden', state.selectedProjects.length === 0);
  }

  container.innerHTML = state.projects.map((p) => {
    const selected = state.selectedProjects.includes(p.id);
    return `
      <div onclick="toggleProjectFilter('${p.id}')"
        class="flex items-center justify-between px-3 py-2 rounded-xl cursor-pointer text-xs transition-all ${
          selected
            ? 'bg-pink-500/15 text-pink-400 font-bold border border-pink-500/30'
            : 'text-zinc-600 dark:text-zinc-300 hover:bg-zinc-200/60 dark:hover:bg-zinc-800/60'
        }">
        <div class="flex items-center gap-2.5 truncate">
          <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${escapeHtml(p.color)}"></span>
          <span class="truncate">${escapeHtml(p.name)}</span>
        </div>
        <input type="checkbox" ${selected ? 'checked' : ''} class="pointer-events-none accent-pink-500 rounded" />
      </div>
    `;
  }).join('');

  const subtitle = document.getElementById('active-view-subtitle');
  if (subtitle) {
    if (state.selectedProjects.length === 0) {
      subtitle.textContent = 'Consultando todos os projetos simultaneamente • America/Sao_Paulo';
    } else {
      const names = state.projects.filter((p) => state.selectedProjects.includes(p.id)).map((p) => p.name);
      subtitle.textContent = `Projetos filtrados (${names.length}): ${names.join(', ')}`;
    }
  }
}

window.toggleProjectFilter = function(projectId) {
  const idx = state.selectedProjects.indexOf(projectId);
  if (idx >= 0) {
    state.selectedProjects.splice(idx, 1);
  } else {
    state.selectedProjects.push(projectId);
  }
  renderSidebarProjects();
  state.page = 1;
  fetchTasks();
};

function populateQuickAddDropdowns() {
  const projSel = document.getElementById('quick-task-project');
  if (projSel) {
    projSel.innerHTML = `<option value="">Sem Projeto</option>` +
      state.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
  }
}

function getAllAvailableColumns() {
  const cols = NATIVE_COLUMNS.slice();
  for (const cf of state.customFields) {
    cols.push({ key: `custom.${cf.key}`, label: `✨ ${cf.name}`, customField: cf });
  }
  return cols;
}

function populateGroupByDropdown() {
  const sel = document.getElementById('view-group-by');
  if (!sel) return;
  const base = [
    { value: '', label: 'Sem agrupamento' },
    { value: 'status_slug', label: 'Status' },
    { value: 'priority', label: 'Prioridade' },
    { value: 'project_id', label: 'Projeto' },
    { value: 'assignee_id', label: 'Responsável' },
    { value: 'due_date', label: 'Data de Vencimento' }
  ];
  for (const cf of state.customFields) {
    base.push({ value: `custom.${cf.key}`, label: `Campo: ${cf.name}` });
  }
  sel.innerHTML = base.map((o) => `<option value="${o.value}">${escapeHtml(o.label)}</option>`).join('');
  sel.value = state.groupBy || '';
}

function renderColumnsSelector() {
  const container = document.getElementById('view-columns-selector');
  if (!container) return;
  const allCols = getAllAvailableColumns();
  container.innerHTML = allCols.map((c) => {
    const isVis = state.visibleColumns.includes(c.key);
    return `
      <button type="button" onclick="toggleVisibleColumn('${c.key}')"
        class="px-2.5 py-1 rounded-lg text-[11px] font-semibold transition-all ${
          isVis
            ? 'bg-pink-500/20 text-pink-400 border border-pink-500/40'
            : 'bg-zinc-200/60 dark:bg-zinc-800/60 text-zinc-400'
        }">
        ${isVis ? '✓ ' : ''}${escapeHtml(c.label)}
      </button>
    `;
  }).join('');
}

window.toggleVisibleColumn = function(colKey) {
  const idx = state.visibleColumns.indexOf(colKey);
  if (idx >= 0) {
    if (state.visibleColumns.length <= 2) return; // keep at least 2 columns
    state.visibleColumns.splice(idx, 1);
  } else {
    state.visibleColumns.push(colKey);
    if (!state.columnOrder.includes(colKey)) {
      state.columnOrder.push(colKey);
    }
  }
  renderColumnsSelector();
  renderTasksViewport();
};

// =====================================================================
// ADVANCED FILTER BUILDER (AND / OR & NESTED GROUPS)
// =====================================================================
function getFilterableFields() {
  const base = [
    { key: 'title', label: 'Título' },
    { key: 'status_slug', label: 'Status' },
    { key: 'priority', label: 'Prioridade' },
    { key: 'project_id', label: 'Projeto' },
    { key: 'assignee_id', label: 'Responsável' },
    { key: 'due_date', label: 'Data de Vencimento' },
    { key: 'tags', label: 'Etiquetas' }
  ];
  for (const cf of state.customFields) {
    base.push({ key: `custom.${cf.key}`, label: `✨ ${cf.name}` });
  }
  return base;
}

const FILTER_OPERATORS = [
  { op: 'eq', label: 'Igual a (=)' },
  { op: 'neq', label: 'Diferente de (≠)' },
  { op: 'contains', label: 'Contém' },
  { op: 'not_contains', label: 'Não contém' },
  { op: 'in', label: 'Está em (lista vírgula)' },
  { op: 'not_in', label: 'Não está em' },
  { op: 'gt', label: 'Maior que (>)' },
  { op: 'gte', label: 'Maior ou igual (≥)' },
  { op: 'lt', label: 'Menor que (<)' },
  { op: 'lte', label: 'Menor ou igual (≤)' },
  { op: 'is_today', label: 'É Hoje' },
  { op: 'is_overdue', label: 'Está Atrasada' },
  { op: 'next_7_days', label: 'Próximos 7 dias' },
  { op: 'is_empty', label: 'Está vazio / Sem data' },
  { op: 'is_not_empty', label: 'Não está vazio' },
  { op: 'is_true', label: 'Checkbox Marcado (Sim)' },
  { op: 'is_false', label: 'Checkbox Desmarcado (Não)' }
];

function countActiveFilterConditions(tree) {
  if (!tree) return 0;
  let c = (tree.conditions || []).length;
  for (const g of tree.groups || []) {
    c += countActiveFilterConditions(g);
  }
  return c;
}

function renderFilterBuilder() {
  const rootLogicEl = document.getElementById('filter-root-logic');
  if (rootLogicEl) rootLogicEl.value = state.filterTree.logic || 'AND';

  const count = countActiveFilterConditions(state.filterTree);
  const badge = document.getElementById('active-filters-count');
  if (badge) {
    badge.textContent = String(count);
    badge.classList.toggle('hidden', count === 0);
  }

  const container = document.getElementById('filter-conditions-container');
  if (!container) return;

  const fields = getFilterableFields();
  let html = '';

  // Render Root Conditions
  (state.filterTree.conditions || []).forEach((cond, idx) => {
    html += renderSingleConditionRow(cond, fields, `root-${idx}`, (field, op, val) => {
      if (field === '__DELETE__') {
        state.filterTree.conditions.splice(idx, 1);
        renderFilterBuilder();
      } else {
        state.filterTree.conditions[idx] = { field, op, value: val };
      }
    });
  });

  // Render Nested Groups
  (state.filterTree.groups || []).forEach((grp, gIdx) => {
    const subCondsHtml = (grp.conditions || []).map((c, cIdx) =>
      renderSingleConditionRow(c, fields, `grp-${gIdx}-${cIdx}`)
    ).join('');

    html += `
      <div class="rounded-2xl border border-purple-500/30 bg-purple-500/5 p-3 space-y-2">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-[11px] font-bold uppercase text-purple-400">Subgrupo #${gIdx + 1}</span>
            <select onchange="updateSubgroupLogic(${gIdx}, this.value)"
              class="rounded-lg bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-0.5 text-xs font-bold">
              <option value="AND" ${grp.logic === 'AND' ? 'selected' : ''}>E (AND)</option>
              <option value="OR" ${grp.logic === 'OR' ? 'selected' : ''}>OU (OR)</option>
            </select>
          </div>
          <div class="flex items-center gap-2">
            <button type="button" onclick="addSubgroupCondition(${gIdx})" class="text-xs text-pink-400 font-semibold">+ Condição no grupo</button>
            <button type="button" onclick="removeSubgroup(${gIdx})" class="text-xs text-rose-400">Remover Grupo</button>
          </div>
        </div>
        <div class="space-y-1.5">${subCondsHtml}</div>
      </div>
    `;
  });

  if (!html) {
    html = `<p class="text-xs text-zinc-400 italic">Nenhuma condição ativa. Clique em "+ Condição" ou "+ Grupo (E/OU)" para filtrar.</p>`;
  }
  container.innerHTML = html;
}

function renderSingleConditionRow(cond, fields, rowId) {
  const noValueOps = ['is_today', 'is_overdue', 'next_7_days', 'is_empty', 'is_not_empty', 'is_true', 'is_false'];
  const hideVal = noValueOps.includes(cond.op);
  const valStr = Array.isArray(cond.value) ? cond.value.join(', ') : (cond.value ?? '');

  return `
    <div class="flex flex-wrap items-center gap-2" data-cond-row="${rowId}">
      <select onchange="handleCondChange('${rowId}', 'field', this.value)"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2.5 py-1.5 text-xs">
        ${fields.map((f) => `<option value="${f.key}" ${cond.field === f.key ? 'selected' : ''}>${escapeHtml(f.label)}</option>`).join('')}
      </select>
      <select onchange="handleCondChange('${rowId}', 'op', this.value)"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2.5 py-1.5 text-xs">
        ${FILTER_OPERATORS.map((o) => `<option value="${o.op}" ${cond.op === o.op ? 'selected' : ''}>${escapeHtml(o.label)}</option>`).join('')}
      </select>
      <input type="text" value="${escapeHtml(valStr)}" placeholder="Valor..."
        onchange="handleCondChange('${rowId}', 'value', this.value)"
        class="${hideVal ? 'hidden' : ''} flex-1 min-w-[140px] rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-1.5 text-xs" />
      <button type="button" onclick="handleCondDelete('${rowId}')"
        class="p-1.5 rounded-xl text-rose-400 hover:bg-rose-500/10 text-xs">✕</button>
    </div>
  `;
}

window.handleCondChange = function(rowId, prop, val) {
  if (rowId.startsWith('root-')) {
    const idx = parseInt(rowId.split('-')[1], 10);
    if (state.filterTree.conditions[idx]) {
      state.filterTree.conditions[idx][prop] = val;
      if (prop === 'op') renderFilterBuilder();
    }
  } else if (rowId.startsWith('grp-')) {
    const parts = rowId.split('-');
    const gIdx = parseInt(parts[1], 10);
    const cIdx = parseInt(parts[2], 10);
    if (state.filterTree.groups[gIdx] && state.filterTree.groups[gIdx].conditions[cIdx]) {
      state.filterTree.groups[gIdx].conditions[cIdx][prop] = val;
      if (prop === 'op') renderFilterBuilder();
    }
  }
};

window.handleCondDelete = function(rowId) {
  if (rowId.startsWith('root-')) {
    const idx = parseInt(rowId.split('-')[1], 10);
    state.filterTree.conditions.splice(idx, 1);
  } else if (rowId.startsWith('grp-')) {
    const parts = rowId.split('-');
    const gIdx = parseInt(parts[1], 10);
    const cIdx = parseInt(parts[2], 10);
    state.filterTree.groups[gIdx].conditions.splice(cIdx, 1);
  }
  renderFilterBuilder();
};

window.updateSubgroupLogic = function(gIdx, logic) {
  if (state.filterTree.groups[gIdx]) {
    state.filterTree.groups[gIdx].logic = logic;
  }
};

window.addSubgroupCondition = function(gIdx) {
  if (state.filterTree.groups[gIdx]) {
    state.filterTree.groups[gIdx].conditions.push({ field: 'priority', op: 'eq', value: 'P1' });
    renderFilterBuilder();
  }
};

window.removeSubgroup = function(gIdx) {
  state.filterTree.groups.splice(gIdx, 1);
  renderFilterBuilder();
};

// =====================================================================
// FETCHING TASKS & UPDATING BENTO METRICS (ZERO-FLICKER)
// =====================================================================
async function fetchTasks() {
  try {
    const payload = {
      filter_tree: state.filterTree,
      search: state.searchQuery,
      project_ids: state.selectedProjects.length ? state.selectedProjects : null,
      sorts: state.sorts,
      group_by: state.groupBy || null,
      page: state.page,
      page_size: state.pageSize
    };
    const res = await api('/api/v1/tasks/query', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    state.tasks = res.items || [];
    state.totalTasks = res.total || 0;
    state.totalPages = res.total_pages || 1;
    state.groups = res.groups || null;

    updateAtomicBentoMetrics();
    renderTasksViewport();
    renderPagination();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function updateAtomicBentoMetrics() {
  const total = state.totalTasks;
  let overdue = 0;
  let today = 0;
  let completed = 0;
  for (const t of state.tasks) {
    if (t.status_slug === 'completed') completed++;
    else if (t.temporal_status === 'overdue') overdue++;
    else if (t.temporal_status === 'today') today++;
  }
  setAtomicText('metric-total', total);
  setAtomicText('metric-overdue', overdue);
  setAtomicText('metric-today', today);
  setAtomicText('metric-completed', completed);
}

function setAtomicText(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const str = String(val);
  if (el.textContent !== str) {
    el.textContent = str;
  }
}

function renderPagination() {
  const info = document.getElementById('pagination-info');
  const lbl = document.getElementById('pagination-page-label');
  const prev = document.getElementById('btn-prev-page');
  const next = document.getElementById('btn-next-page');
  if (info) info.textContent = `Exibindo ${state.tasks.length} de ${state.totalTasks} tarefas`;
  if (lbl) lbl.textContent = `Página ${state.page} de ${state.totalPages}`;
  if (prev) prev.disabled = state.page <= 1;
  if (next) next.disabled = state.page >= state.totalPages;
}

// =====================================================================
// VIEWPORT RENDERING (1. TABLE + MOBILE CARDS, 2. KANBAN, 3. LIST)
// =====================================================================
function updateViewModeButtons() {
  const labels = { table: 'Tabela', kanban: 'Kanban', list: 'Lista' };
  const badge = document.getElementById('active-view-badge');
  if (badge) badge.textContent = labels[state.viewMode] || 'Tabela';

  document.querySelectorAll('.btn-view-mode').forEach((btn) => {
    const mode = btn.getAttribute('data-view-mode');
    if (mode === state.viewMode) {
      btn.className = 'btn-view-mode px-3 py-1.5 rounded-xl text-xs font-bold bg-gradient-to-r from-orange-500 via-pink-500 to-purple-600 text-white shadow transition-all flex-1 sm:flex-initial text-center';
    } else {
      btn.className = 'btn-view-mode px-3 py-1.5 rounded-xl text-xs font-semibold text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-white transition-all flex-1 sm:flex-initial text-center';
    }
  });
}

function renderTasksViewport() {
  const vp = document.getElementById('tasks-viewport');
  if (!vp) return;

  if (state.viewMode === 'kanban') {
    vp.innerHTML = renderKanbanView();
    return;
  }
  if (state.viewMode === 'list') {
    vp.innerHTML = renderListView();
    return;
  }
  vp.innerHTML = renderTableView();
}

function getPriorityBadge(priority) {
  const map = {
    P1: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
    P2: 'bg-orange-500/15 text-orange-400 border-orange-500/30',
    P3: 'bg-pink-500/15 text-pink-400 border-pink-500/30',
    P4: 'bg-purple-500/15 text-purple-400 border-purple-500/30'
  };
  return `<span class="px-2 py-0.5 rounded-lg text-[10px] font-extrabold border ${map[priority] || map.P3}">${escapeHtml(priority)}</span>`;
}

function getDueBadge(task) {
  if (!task.due_date) {
    return `<span class="text-[11px] text-zinc-400 italic">Sem data</span>`;
  }
  const styles = {
    overdue: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
    today: 'bg-orange-500/15 text-orange-400 border-orange-500/30',
    upcoming: 'bg-zinc-200/70 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300 border-zinc-300 dark:border-zinc-700',
    completed: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
  };
  const cls = styles[task.temporal_status] || styles.upcoming;
  const timeStr = task.due_time ? ` ${task.due_time}` : '';
  return `<span class="px-2 py-0.5 rounded-lg text-[11px] font-semibold border ${cls}">${escapeHtml(task.due_date)}${escapeHtml(timeStr)}</span>`;
}

// --- 1. TABLE VIEW (Inline Editable on Desktop + Adaptive Responsive Cards on Mobile) ---
function renderTableView() {
  if (!state.tasks.length) {
    return renderEmptyState();
  }

  const orderedVisibleCols = state.columnOrder.filter((k) => state.visibleColumns.includes(k));
  const allColsMap = {};
  for (const c of getAllAvailableColumns()) allColsMap[c.key] = c;

  if (state.groupBy && state.groups) {
    return Object.entries(state.groups).map(([grpName, items]) => `
      <div class="mb-6">
        <div class="flex items-center gap-2 mb-2 px-2">
          <span class="text-xs font-extrabold uppercase tracking-wider text-pink-500">${escapeHtml(grpName)}</span>
          <span class="text-[10px] px-2 py-0.5 rounded-full bg-zinc-200 dark:bg-zinc-800 font-bold">${items.length}</span>
        </div>
        ${renderTableMarkup(items, orderedVisibleCols, allColsMap)}
      </div>
    `).join('');
  }

  return renderTableMarkup(state.tasks, orderedVisibleCols, allColsMap);
}

function renderTableMarkup(tasksList, orderedVisibleCols, allColsMap) {
  // Desktop Table + Mobile Adaptive Cards
  const headersHtml = orderedVisibleCols.map((colKey, idx) => {
    const colDef = allColsMap[colKey] || { key: colKey, label: colKey };
    return `
      <th class="p-3 text-[11px] font-extrabold uppercase tracking-wider text-zinc-400 whitespace-nowrap border-b border-zinc-200 dark:border-zinc-800">
        <div class="flex items-center gap-1.5">
          <span>${escapeHtml(colDef.label)}</span>
          <div class="flex items-center opacity-50 hover:opacity-100">
            ${idx > 0 ? `<button type="button" onclick="moveColumnOrder('${colKey}', -1)" title="Mover p/ esquerda" class="px-1 hover:text-pink-400">←</button>` : ''}
            ${idx < orderedVisibleCols.length - 1 ? `<button type="button" onclick="moveColumnOrder('${colKey}', 1)" title="Mover p/ direita" class="px-1 hover:text-pink-400">→</button>` : ''}
          </div>
        </div>
      </th>
    `;
  }).join('');

  const rowsHtml = tasksList.map((t) => {
    const isDone = t.status_slug === 'completed';
    const cellsHtml = orderedVisibleCols.map((colKey) => {
      return `<td class="p-2.5 text-xs border-b border-zinc-200/60 dark:border-zinc-800/60 align-middle">${renderTableCell(t, colKey)}</td>`;
    }).join('');

    return `
      <tr class="group hover:bg-zinc-100/70 dark:hover:bg-zinc-900/70 transition-colors ${isDone ? 'opacity-65' : ''}"
          style="${t.color ? `border-left: 3px solid ${escapeHtml(t.color)}` : ''}">
        <td class="p-2.5 border-b border-zinc-200/60 dark:border-zinc-800/60 w-9 text-center">
          <input type="checkbox" ${isDone ? 'checked' : ''}
            onchange="quickToggleTaskComplete('${t.id}', this.checked)"
            class="w-4 h-4 rounded accent-pink-500 cursor-pointer" />
        </td>
        ${cellsHtml}
        <td class="p-2.5 border-b border-zinc-200/60 dark:border-zinc-800/60 text-right whitespace-nowrap">
          <button onclick="openTaskModal('${t.id}')" class="px-2.5 py-1 rounded-xl bg-zinc-200/70 dark:bg-zinc-800 text-[11px] font-semibold hover:bg-pink-500 hover:text-white transition-all">
            Abrir
          </button>
        </td>
      </tr>
    `;
  }).join('');

  // Mobile Adaptive Card Rows (shown on small screens < md so table never overflows awkwardly)
  const mobileCardsHtml = tasksList.map((t) => renderMobileTaskCard(t, orderedVisibleCols, allColsMap)).join('');

  return `
    <!-- Desktop Table -->
    <div class="hidden md:block overflow-x-auto rounded-3xl border border-zinc-200/80 dark:border-zinc-800/80 bg-zinc-50/60 dark:bg-zinc-900/40 backdrop-blur-md shadow-sm">
      <table class="w-full text-left border-collapse">
        <thead>
          <tr class="bg-zinc-100/60 dark:bg-zinc-950/60">
            <th class="p-3 w-9 border-b border-zinc-200 dark:border-zinc-800">✓</th>
            ${headersHtml}
            <th class="p-3 text-right border-b border-zinc-200 dark:border-zinc-800 text-[11px] uppercase text-zinc-400">Ações</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>

    <!-- Mobile Adaptive Table-Cards -->
    <div class="md:hidden space-y-2.5">
      ${mobileCardsHtml}
    </div>
  `;
}

function renderTableCell(task, colKey) {
  if (colKey === 'code') {
    return `<span onclick="openTaskModal('${task.id}')" class="font-mono font-bold text-orange-400 cursor-pointer hover:underline">#${task.code}</span>`;
  }
  if (colKey === 'title') {
    return `
      <input type="text" value="${escapeHtml(task.title)}"
        onchange="inlinePatchTask('${task.id}', { title: this.value })"
        class="w-full min-w-[180px] bg-transparent font-semibold text-zinc-900 dark:text-zinc-100 focus:bg-white dark:focus:bg-zinc-950 focus:px-2 focus:py-1 rounded-lg focus:outline-none focus:ring-1 focus:ring-pink-500 ${task.status_slug === 'completed' ? 'line-through text-zinc-400' : ''}" />
    `;
  }
  if (colKey === 'status_slug') {
    return `
      <select onchange="inlinePatchTask('${task.id}', { status_slug: this.value })"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-xs font-semibold">
        ${state.statuses.map((s) => `<option value="${s.slug}" ${task.status_slug === s.slug ? 'selected' : ''}>${escapeHtml(s.name)}</option>`).join('')}
      </select>
    `;
  }
  if (colKey === 'priority') {
    return `
      <select onchange="inlinePatchTask('${task.id}', { priority: this.value })"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-xs font-bold">
        ${['P1', 'P2', 'P3', 'P4'].map((p) => `<option value="${p}" ${task.priority === p ? 'selected' : ''}>${p}</option>`).join('')}
      </select>
    `;
  }
  if (colKey === 'project_id') {
    return `
      <select onchange="inlinePatchTask('${task.id}', { project_id: this.value || null })"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-xs">
        <option value="">Sem Projeto</option>
        ${state.projects.map((p) => `<option value="${p.id}" ${task.project_id === p.id ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
      </select>
    `;
  }
  if (colKey === 'assignee_id') {
    return `
      <select onchange="inlinePatchTask('${task.id}', { assignee_id: this.value || null })"
        class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-xs">
        <option value="">Sem Responsável</option>
        ${state.users.map((u) => `<option value="${u.id}" ${task.assignee_id === u.id ? 'selected' : ''}>${escapeHtml(u.full_name || u.username)}</option>`).join('')}
      </select>
    `;
  }
  if (colKey === 'due_date') {
    return `
      <div class="flex items-center gap-1.5">
        <input type="date" value="${escapeHtml(task.due_date || '')}"
          onchange="inlinePatchTask('${task.id}', { due_date: this.value || null })"
          class="rounded-xl bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-xs" />
      </div>
    `;
  }
  if (colKey === 'tags') {
    const tags = task.tags || [];
    if (!tags.length) return `<span class="text-zinc-400">-</span>`;
    return `<div class="flex flex-wrap gap-1">${tags.map((tg) => `<span class="px-2 py-0.5 rounded-full bg-pink-500/10 text-pink-400 text-[10px] font-semibold">#${escapeHtml(tg)}</span>`).join('')}</div>`;
  }
  if (colKey === 'subtasks') {
    return `<span class="text-xs font-mono">${task.subtasks_completed || 0}/${task.subtasks_total || 0}</span>`;
  }
  if (colKey.startsWith('custom.')) {
    const cfKey = colKey.split('custom.')[1];
    const cf = state.customFields.find((f) => f.key === cfKey);
    const val = (task.custom_values || {})[cfKey];
    if (!cf) return escapeHtml(val ?? '-');

    if (cf.field_type === 'checkbox') {
      return `
        <input type="checkbox" ${val ? 'checked' : ''}
          onchange="inlinePatchCustomField('${task.id}', '${cfKey}', this.checked)"
          class="w-4 h-4 accent-pink-500 rounded cursor-pointer" />
      `;
    }
    if (cf.field_type === 'url' && val) {
      return `<a href="${escapeHtml(val)}" target="_blank" class="text-pink-400 underline truncate block max-w-[160px]">${escapeHtml(val)}</a>`;
    }
    if (cf.field_type === 'currency' && val !== undefined && val !== null && val !== '') {
      return `<span class="font-mono font-semibold text-emerald-400">R$ ${Number(val).toFixed(2)}</span>`;
    }
    return `
      <input type="text" value="${escapeHtml(Array.isArray(val) ? val.join(', ') : (val ?? ''))}"
        onchange="inlinePatchCustomField('${task.id}', '${cfKey}', this.value)"
        placeholder="-"
        class="w-full min-w-[110px] bg-transparent border-b border-transparent focus:border-pink-500 focus:outline-none text-xs" />
    `;
  }
  return escapeHtml(task[colKey] ?? '-');
}

function renderMobileTaskCard(t, orderedVisibleCols, allColsMap) {
  const isDone = t.status_slug === 'completed';
  const customPills = orderedVisibleCols
    .filter((k) => k.startsWith('custom.'))
    .map((k) => {
      const cfKey = k.split('custom.')[1];
      const val = (t.custom_values || {})[cfKey];
      if (val === undefined || val === null || val === '') return '';
      const label = (allColsMap[k] && allColsMap[k].label) || cfKey;
      return `<span class="px-2 py-0.5 rounded-lg bg-zinc-200/70 dark:bg-zinc-800 text-[10px]">${escapeHtml(label)}: <b>${escapeHtml(val)}</b></span>`;
    })
    .filter(Boolean)
    .join('');

  return `
    <div class="rounded-2xl p-3.5 bg-zinc-50 dark:bg-zinc-900/70 border border-zinc-200/80 dark:border-zinc-800/80 shadow-sm flex flex-col gap-2"
         style="${t.color ? `border-left: 4px solid ${escapeHtml(t.color)}` : ''}">
      <div class="flex items-start justify-between gap-2">
        <div class="flex items-start gap-2.5 min-w-0">
          <input type="checkbox" ${isDone ? 'checked' : ''}
            onchange="quickToggleTaskComplete('${t.id}', this.checked)"
            class="mt-1 w-4 h-4 rounded accent-pink-500 shrink-0" />
          <div onclick="openTaskModal('${t.id}')" class="cursor-pointer min-w-0">
            <div class="flex items-center gap-1.5">
              <span class="font-mono text-[11px] font-bold text-orange-400">#${t.code}</span>
              ${getPriorityBadge(t.priority)}
              ${t.project_name ? `<span class="text-[10px] px-2 py-0.5 rounded-full bg-zinc-200 dark:bg-zinc-800 truncate">${escapeHtml(t.project_name)}</span>` : ''}
            </div>
            <p class="text-sm font-bold mt-1 ${isDone ? 'line-through text-zinc-400' : ''}">${escapeHtml(t.title)}</p>
          </div>
        </div>
        <button onclick="openTaskModal('${t.id}')" class="text-xs text-zinc-400 px-1.5 py-0.5">✎</button>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-zinc-200/50 dark:border-zinc-800/60 text-xs">
        <select onchange="inlinePatchTask('${t.id}', { status_slug: this.value })"
          class="rounded-lg bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-2 py-1 text-[11px] font-semibold">
          ${state.statuses.map((s) => `<option value="${s.slug}" ${t.status_slug === s.slug ? 'selected' : ''}>${escapeHtml(s.name)}</option>`).join('')}
        </select>
        <div class="flex items-center gap-2">
          ${t.subtasks_total > 0 ? `<span class="text-[11px] text-zinc-400">☑️ ${t.subtasks_completed}/${t.subtasks_total}</span>` : ''}
          ${getDueBadge(t)}
        </div>
      </div>
      ${customPills ? `<div class="flex flex-wrap gap-1 pt-1">${customPills}</div>` : ''}
    </div>
  `;
}

// --- 2. KANBAN VIEW (Drag and Drop Columns) ---
function renderKanbanView() {
  const colsHtml = state.statuses.map((st) => {
    const colTasks = state.tasks.filter((t) => t.status_slug === st.slug);
    const cardsHtml = colTasks.map((t) => {
      const isDone = t.status_slug === 'completed';
      return `
        <div draggable="true"
             ondragstart="handleKanbanDragStart(event, '${t.id}')"
             onclick="openTaskModal('${t.id}')"
             class="cursor-grab active:cursor-grabbing rounded-2xl p-3.5 bg-white dark:bg-zinc-900 border border-zinc-200/80 dark:border-zinc-800/80 shadow-sm hover:-translate-y-0.5 hover:border-pink-500/40 transition-all duration-300 space-y-2.5"
             style="${t.color ? `border-left: 4px solid ${escapeHtml(t.color)}` : ''}">
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-1.5">
              <span class="font-mono text-[11px] font-bold text-orange-400">#${t.code}</span>
              ${getPriorityBadge(t.priority)}
            </div>
            <input type="checkbox" ${isDone ? 'checked' : ''}
              onclick="event.stopPropagation()"
              onchange="quickToggleTaskComplete('${t.id}', this.checked)"
              class="w-4 h-4 rounded accent-pink-500 cursor-pointer" />
          </div>
          <h4 class="text-xs font-bold leading-snug ${isDone ? 'line-through text-zinc-400' : ''}">${escapeHtml(t.title)}</h4>
          ${t.project_name ? `<div class="text-[10px] font-semibold text-zinc-400 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full" style="background:${escapeHtml(t.project_color)}"></span>${escapeHtml(t.project_name)}</div>` : ''}
          <div class="flex items-center justify-between pt-2 border-t border-zinc-100 dark:border-zinc-800/60">
            ${getDueBadge(t)}
            <div class="flex items-center gap-2 text-[11px] text-zinc-400">
              ${t.subtasks_total ? `<span>☑️ ${t.subtasks_completed}/${t.subtasks_total}</span>` : ''}
              ${t.comments_count ? `<span>💬 ${t.comments_count}</span>` : ''}
              ${t.attachments_count ? `<span>📎 ${t.attachments_count}</span>` : ''}
            </div>
          </div>
        </div>
      `;
    }).join('');

    return `
      <div class="w-80 shrink-0 flex flex-col rounded-3xl bg-zinc-100/80 dark:bg-zinc-900/40 border border-zinc-200/80 dark:border-zinc-800/80 p-3 min-h-[420px] transition-all"
           ondragover="handleKanbanDragOver(event)"
           ondragleave="handleKanbanDragLeave(event)"
           ondrop="handleKanbanDrop(event, '${st.slug}')">
        <div class="flex items-center justify-between px-2 pb-3 mb-2 border-b border-zinc-200/60 dark:border-zinc-800/60">
          <div class="flex items-center gap-2">
            <span class="w-3 h-3 rounded-full" style="background:${escapeHtml(st.color)}"></span>
            <h3 class="text-xs font-extrabold uppercase tracking-wider">${escapeHtml(st.name)}</h3>
          </div>
          <span class="text-xs font-mono font-bold px-2 py-0.5 rounded-full bg-white dark:bg-zinc-800 text-zinc-400">${colTasks.length}</span>
        </div>
        <div class="flex-1 space-y-2.5">${cardsHtml}</div>
      </div>
    `;
  }).join('');

  return `<div class="flex gap-4 overflow-x-auto pb-4 items-start">${colsHtml}</div>`;
}

window.handleKanbanDragStart = function(e, taskId) {
  e.dataTransfer.setData('text/plain', taskId);
};
window.handleKanbanDragOver = function(e) {
  e.preventDefault();
  e.currentTarget.classList.add('kanban-drag-over');
};
window.handleKanbanDragLeave = function(e) {
  e.currentTarget.classList.remove('kanban-drag-over');
};
window.handleKanbanDrop = async function(e, targetStatusSlug) {
  e.preventDefault();
  e.currentTarget.classList.remove('kanban-drag-over');
  const taskId = e.dataTransfer.getData('text/plain');
  if (!taskId) return;
  await inlinePatchTask(taskId, { status_slug: targetStatusSlug });
};

// --- 3. LIST VIEW (Compact Modern Stream) ---
function renderListView() {
  if (!state.tasks.length) return renderEmptyState();
  return `
    <div class="space-y-2">
      ${state.tasks.map((t) => {
        const isDone = t.status_slug === 'completed';
        return `
          <div class="rounded-2xl px-4 py-3 bg-zinc-50 dark:bg-zinc-900/60 border border-zinc-200/80 dark:border-zinc-800/80 hover:border-pink-500/40 flex flex-wrap items-center justify-between gap-3 transition-all"
               style="${t.color ? `border-left: 4px solid ${escapeHtml(t.color)}` : ''}">
            <div class="flex items-center gap-3 min-w-0 flex-1">
              <input type="checkbox" ${isDone ? 'checked' : ''}
                onchange="quickToggleTaskComplete('${t.id}', this.checked)"
                class="w-4 h-4 rounded accent-pink-500 cursor-pointer shrink-0" />
              <span class="font-mono text-xs font-bold text-orange-400 shrink-0">#${t.code}</span>
              ${getPriorityBadge(t.priority)}
              <span onclick="openTaskModal('${t.id}')"
                class="text-sm font-bold truncate cursor-pointer hover:text-pink-400 ${isDone ? 'line-through text-zinc-400' : ''}">
                ${escapeHtml(t.title)}
              </span>
            </div>
            <div class="flex items-center gap-2.5 text-xs">
              ${t.project_name ? `<span class="px-2.5 py-0.5 rounded-full bg-zinc-200/70 dark:bg-zinc-800 text-[11px]">${escapeHtml(t.project_name)}</span>` : ''}
              ${t.assignee_name ? `<span class="text-[11px] text-zinc-400">👤 ${escapeHtml(t.assignee_name)}</span>` : ''}
              ${getDueBadge(t)}
              <button onclick="openTaskModal('${t.id}')" class="px-2.5 py-1 rounded-xl bg-zinc-200 dark:bg-zinc-800 text-[11px] font-semibold">Detalhes</button>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderEmptyState() {
  return `
    <div class="rounded-3xl border border-dashed border-zinc-300 dark:border-zinc-800 p-12 text-center space-y-3">
      <div class="text-3xl">✨</div>
      <h3 class="text-sm font-bold">Nenhuma tarefa encontrada para este filtro</h3>
      <p class="text-xs text-zinc-400 max-w-md mx-auto">Use a barra de adição rápida acima ou ajuste os filtros para visualizar tarefas de outros status e projetos.</p>
    </div>
  `;
}

// =====================================================================
// INLINE ACTIONS & COLUMN REORDERING
// =====================================================================
window.moveColumnOrder = function(colKey, delta) {
  const vis = state.columnOrder.filter((k) => state.visibleColumns.includes(k));
  const idx = vis.indexOf(colKey);
  const targetIdx = idx + delta;
  if (idx < 0 || targetIdx < 0 || targetIdx >= vis.length) return;
  const swapKey = vis[targetIdx];
  const fullA = state.columnOrder.indexOf(colKey);
  const fullB = state.columnOrder.indexOf(swapKey);
  state.columnOrder[fullA] = swapKey;
  state.columnOrder[fullB] = colKey;
  renderTasksViewport();
};

window.quickToggleTaskComplete = async function(taskId, checked) {
  const newStatus = checked ? 'completed' : 'pending';
  await inlinePatchTask(taskId, { status_slug: newStatus });
};

window.inlinePatchTask = async function(taskId, patchObj) {
  try {
    await api(`/api/v1/tasks/${taskId}`, {
      method: 'PATCH',
      body: JSON.stringify(patchObj)
    });
    await fetchTasks();
  } catch (e) {
    showToast(e.message, 'error');
    await fetchTasks();
  }
};

window.inlinePatchCustomField = async function(taskId, cfKey, newVal) {
  await inlinePatchTask(taskId, { custom_values: { [cfKey]: newVal } });
};

// =====================================================================
// TASK DETAIL MODAL (Create / Edit / Subtasks / Reminders / Files / Comments / History)
// =====================================================================
window.openTaskModal = async function(taskId = null, showModal = true) {
  state.activeTaskModalId = taskId;
  const modal = document.getElementById('task-modal');
  const extras = document.getElementById('modal-existing-extras');
  const delBtn = document.getElementById('btn-delete-task-modal');

  // Populate dropdowns
  document.getElementById('form-task-status').innerHTML = state.statuses
    .map((s) => `<option value="${s.slug}">${escapeHtml(s.name)}</option>`).join('');
  document.getElementById('form-task-project').innerHTML = `<option value="">Sem Projeto</option>` +
    state.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
  document.getElementById('form-task-assignee').innerHTML = `<option value="">Sem Responsável</option>` +
    state.users.map((u) => `<option value="${u.id}">${escapeHtml(u.full_name || u.username)}</option>`).join('');
  document.getElementById('form-task-participants').innerHTML = state.users
    .map((u) => `<option value="${u.id}">${escapeHtml(u.full_name || u.username)}</option>`).join('');

  if (!taskId) {
    document.getElementById('form-task-id').value = '';
    document.getElementById('modal-task-code').textContent = '#NOVA';
    document.getElementById('modal-header-title').textContent = 'Criar Nova Tarefa';
    document.getElementById('form-task-title').value = '';
    document.getElementById('form-task-color').value = '#f97316';
    document.getElementById('form-task-status').value = 'pending';
    document.getElementById('form-task-priority').value = 'P3';
    document.getElementById('form-task-project').value = state.selectedProjects[0] || '';
    document.getElementById('form-task-assignee').value = state.user ? state.user.id : '';
    document.getElementById('form-task-due-date').value = '';
    document.getElementById('form-task-due-time').value = '';
    document.getElementById('form-task-recurrence').value = '';
    document.getElementById('form-task-tags').value = '';
    document.getElementById('form-task-description').value = '';
    document.getElementById('form-task-notes').value = '';
    renderModalCustomFields({});
    extras.classList.add('hidden');
    delBtn.classList.add('hidden');
    if (showModal) modal.classList.remove('hidden');
    return;
  }

  try {
    const res = await api(`/api/v1/tasks/${taskId}`);
    const t = res.task;
    document.getElementById('form-task-id').value = t.id;
    document.getElementById('modal-task-code').textContent = `#${t.code}`;
    document.getElementById('modal-header-title').textContent = t.title;
    document.getElementById('form-task-title').value = t.title;
    document.getElementById('form-task-color').value = t.color || '#f97316';
    document.getElementById('form-task-status').value = t.status_slug;
    document.getElementById('form-task-priority').value = t.priority;
    document.getElementById('form-task-project').value = t.project_id || '';
    document.getElementById('form-task-assignee').value = t.assignee_id || '';
    document.getElementById('form-task-due-date').value = t.due_date || '';
    document.getElementById('form-task-due-time').value = t.due_time || '';
    document.getElementById('form-task-recurrence').value = (t.recurrence && t.recurrence.frequency) || '';
    document.getElementById('form-task-tags').value = (t.tags || []).join(', ');
    document.getElementById('form-task-description').value = t.description || '';
    document.getElementById('form-task-notes').value = t.notes || '';

    const partSel = document.getElementById('form-task-participants');
    Array.from(partSel.options).forEach((opt) => {
      opt.selected = (t.participants || []).includes(opt.value);
    });

    renderModalCustomFields(t.custom_values || {});
    renderModalExtras(t);
    extras.classList.remove('hidden');
    delBtn.classList.remove('hidden');
    if (showModal) modal.classList.remove('hidden');
  } catch (e) {
    showToast(e.message, 'error');
  }
};

function renderModalCustomFields(values) {
  const container = document.getElementById('modal-custom-fields-container');
  const wrapper = document.getElementById('modal-custom-fields-wrapper');
  if (!container || !wrapper) return;
  if (!state.customFields.length) {
    wrapper.classList.add('hidden');
    return;
  }
  wrapper.classList.remove('hidden');

  container.innerHTML = state.customFields.map((cf) => {
    const val = values[cf.key] !== undefined ? values[cf.key] : (cf.default_value ?? '');
    const reqAttr = cf.required ? 'required' : '';
    const reqStar = cf.required ? ' *' : '';
    let inputHtml = '';

    if (cf.field_type === 'long_text') {
      inputHtml = `<textarea data-cf-key="${cf.key}" rows="2" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 p-2 text-xs">${escapeHtml(val)}</textarea>`;
    } else if (cf.field_type === 'number' || cf.field_type === 'currency') {
      inputHtml = `<input type="number" step="any" data-cf-key="${cf.key}" value="${escapeHtml(val)}" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />`;
    } else if (cf.field_type === 'date') {
      inputHtml = `<input type="date" data-cf-key="${cf.key}" value="${escapeHtml(val)}" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />`;
    } else if (cf.field_type === 'datetime') {
      inputHtml = `<input type="datetime-local" data-cf-key="${cf.key}" value="${escapeHtml(val)}" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />`;
    } else if (cf.field_type === 'checkbox') {
      inputHtml = `<label class="flex items-center gap-2 text-xs py-2"><input type="checkbox" data-cf-key="${cf.key}" ${val ? 'checked' : ''} class="w-4 h-4 accent-pink-500 rounded" /> Ativo / Sim</label>`;
    } else if (cf.field_type === 'select') {
      inputHtml = `
        <select data-cf-key="${cf.key}" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs">
          <option value="">Selecione...</option>
          ${(cf.options || []).map((o) => `<option value="${escapeHtml(o)}" ${val === o ? 'selected' : ''}>${escapeHtml(o)}</option>`).join('')}
        </select>
      `;
    } else if (cf.field_type === 'multi_select') {
      const valStr = Array.isArray(val) ? val.join(', ') : (val || '');
      inputHtml = `<input type="text" data-cf-key="${cf.key}" value="${escapeHtml(valStr)}" placeholder="Opções: ${(cf.options || []).join(', ')}" class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />`;
    } else {
      inputHtml = `<input type="${cf.field_type === 'url' ? 'url' : 'text'}" data-cf-key="${cf.key}" value="${escapeHtml(val)}" ${reqAttr} class="w-full rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />`;
    }

    return `
      <div>
        <label class="block text-[11px] font-bold uppercase text-zinc-400 mb-1">${escapeHtml(cf.name)}${reqStar} <span class="text-[9px] text-zinc-500">(${cf.field_type})</span></label>
        ${inputHtml}
      </div>
    `;
  }).join('');
}

function renderModalExtras(t) {
  // Subtasks
  const stEl = document.getElementById('modal-subtasks-list');
  stEl.innerHTML = (t.subtasks || []).map((s) => `
    <div class="flex items-center justify-between px-3 py-1.5 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
      <label class="flex items-center gap-2 cursor-pointer flex-1">
        <input type="checkbox" ${s.is_completed ? 'checked' : ''}
          onchange="toggleSubtask('${t.id}', '${s.id}', this.checked)"
          class="w-4 h-4 accent-pink-500 rounded" />
        <span class="${s.is_completed ? 'line-through text-zinc-400' : ''}">${escapeHtml(s.title)}</span>
      </label>
      <button type="button" onclick="removeSubtask('${t.id}', '${s.id}')" class="text-rose-400 hover:underline text-[11px]">Excluir</button>
    </div>
  `).join('') || `<p class="text-xs text-zinc-400 italic">Nenhuma subtarefa cadastrada.</p>`;

  // Reminders
  const remEl = document.getElementById('modal-reminders-list');
  remEl.innerHTML = (t.reminders || []).map((r) => `
    <div class="flex items-center justify-between px-3 py-1.5 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
      <span>⏰ <b>${escapeHtml(r.remind_at)}</b> — ${escapeHtml(r.message || 'Lembrete')} ${r.is_sent ? '<span class="text-emerald-400">(Enviado)</span>' : '<span class="text-amber-400">(Pendente)</span>'}</span>
      <button type="button" onclick="removeReminder('${r.id}')" class="text-rose-400 text-[11px]">Remover</button>
    </div>
  `).join('') || `<p class="text-xs text-zinc-400 italic">Nenhum lembrete configurado para esta tarefa.</p>`;

  // Attachments
  const attEl = document.getElementById('modal-attachments-list');
  attEl.innerHTML = (t.attachments || []).map((a) => `
    <div class="flex items-center justify-between px-3 py-1.5 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
      <a href="/api/v1/attachments/${a.id}/download?token=${encodeURIComponent(state.token)}" target="_blank" class="text-pink-400 hover:underline font-medium">
        📎 ${escapeHtml(a.filename)} (${Math.round((a.size_bytes || 0) / 1024)} KB)
      </a>
      <button type="button" onclick="removeAttachment('${t.id}', '${a.id}')" class="text-rose-400 text-[11px]">Excluir</button>
    </div>
  `).join('') || `<p class="text-xs text-zinc-400 italic">Nenhum anexo enviado.</p>`;

  // Comments
  const comEl = document.getElementById('modal-comments-list');
  comEl.innerHTML = (t.comments || []).map((c) => `
    <div class="p-2.5 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
      <div class="flex items-center justify-between text-[10px] text-zinc-400 mb-1">
        <span class="font-bold text-pink-400">${escapeHtml(c.author_name)}</span>
        <span>${escapeHtml((c.created_at || '').slice(0, 19).replace('T', ' '))}</span>
      </div>
      <p>${escapeHtml(c.content)}</p>
    </div>
  `).join('') || `<p class="text-xs text-zinc-400 italic">Sem comentários ainda.</p>`;

  // History
  const histEl = document.getElementById('modal-history-list');
  histEl.innerHTML = (t.history || []).map((h) => `
    <div class="flex items-start justify-between gap-2 border-b border-zinc-200/40 dark:border-zinc-800/40 pb-1">
      <span><b>${escapeHtml(h.actor_name)}</b> executou <code>${escapeHtml(h.action)}</code>: ${escapeHtml(JSON.stringify(h.changes))}</span>
      <span class="shrink-0 font-mono text-[10px]">${escapeHtml((h.created_at || '').slice(0, 19).replace('T', ' '))}</span>
    </div>
  `).join('');
}

window.toggleSubtask = async function(taskId, subtaskId, isCompleted) {
  await api(`/api/v1/tasks/${taskId}/subtasks/${subtaskId}`, {
    method: 'PATCH',
    body: JSON.stringify({ is_completed: isCompleted })
  });
  await openTaskModal(taskId, false);
  await fetchTasks();
};

window.removeSubtask = async function(taskId, subtaskId) {
  await api(`/api/v1/tasks/${taskId}/subtasks/${subtaskId}`, { method: 'DELETE' });
  await openTaskModal(taskId, false);
  await fetchTasks();
};

window.removeReminder = async function(remId) {
  await api(`/api/v1/reminders/${remId}`, { method: 'DELETE' });
  if (state.activeTaskModalId) await openTaskModal(state.activeTaskModalId, false);
};

window.removeAttachment = async function(taskId, attId) {
  await api(`/api/v1/tasks/${taskId}/attachments/${attId}`, { method: 'DELETE' });
  await openTaskModal(taskId, false);
  await fetchTasks();
};

// =====================================================================
// NOTIFICATIONS & WEB PUSH VAPID SUBSCRIPTION
// =====================================================================
async function refreshNotificationBadge() {
  try {
    const res = await api('/api/v1/notifications?limit=20');
    const badge = document.getElementById('notif-unread-badge');
    if (badge) {
      badge.textContent = String(res.unread_count || 0);
      badge.classList.toggle('hidden', !res.unread_count);
    }
  } catch (e) {}
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

window.subscribeWebPushDevice = async function() {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      showToast('Seu navegador não suporta Web Push API.', 'error');
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      showToast('Permissão de notificação negada no navegador.', 'error');
      return;
    }
    const vapidRes = await api('/api/v1/push/vapid-public-key');
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidRes.public_key)
    });
    const subJson = sub.toJSON();
    await api('/api/v1/push/subscribe', {
      method: 'POST',
      body: JSON.stringify({
        endpoint: subJson.endpoint,
        keys: subJson.keys,
        user_agent: navigator.userAgent
      })
    });
    showToast('Dispositivo inscrito no Web Push VAPID com sucesso!', 'success');
  } catch (e) {
    showToast(`Aviso Web Push: ${e.message}`, 'error');
  }
};

// =====================================================================
// ADMIN & SETTINGS PANEL (7 Tabs)
// =====================================================================
window.openAdminModal = async function(tabName = 'fields') {
  state.activeAdminTab = tabName;
  document.getElementById('admin-modal').classList.remove('hidden');
  document.querySelectorAll('.admin-tab-btn').forEach((btn) => {
    const t = btn.getAttribute('data-tab');
    if (t === tabName) {
      btn.className = 'admin-tab-btn py-3 px-3 text-xs font-extrabold border-b-2 border-pink-500 text-pink-500 whitespace-nowrap';
    } else {
      btn.className = 'admin-tab-btn py-3 px-3 text-xs font-bold border-b-2 border-transparent text-zinc-400 hover:text-zinc-200 whitespace-nowrap';
    }
  });
  await renderAdminTabContent(tabName);
};

async function renderAdminTabContent(tab) {
  const container = document.getElementById('admin-tab-content');
  if (!container) return;

  if (tab === 'fields') {
    container.innerHTML = `
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Custom Fields Management -->
        <div class="space-y-4">
          <h4 class="text-sm font-extrabold text-orange-400">🧩 Criar Campo Personalizado</h4>
          <form onsubmit="handleCreateCustomField(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
            <div class="grid grid-cols-2 gap-2">
              <input id="cf-name" type="text" required placeholder="Nome (ex: Orçamento)" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
              <select id="cf-type" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs">
                <option value="short_text">Texto Curto</option>
                <option value="long_text">Texto Longo</option>
                <option value="number">Número</option>
                <option value="currency">Moeda (R$)</option>
                <option value="date">Data</option>
                <option value="datetime">Data e Hora</option>
                <option value="checkbox">Checkbox</option>
                <option value="select">Seleção Única (Listbox)</option>
                <option value="multi_select">Múltipla Seleção</option>
                <option value="url">URL</option>
              </select>
            </div>
            <input id="cf-options" type="text" placeholder="Opções p/ Listbox (separadas por vírgula)" class="w-full rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <div class="flex items-center justify-between">
              <label class="flex items-center gap-2 text-xs"><input id="cf-req" type="checkbox" class="accent-pink-500" /> Campo Obrigatório</label>
              <button type="submit" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">+ Criar Campo</button>
            </div>
          </form>

          <div class="space-y-2">
            ${state.customFields.map((cf) => `
              <div class="flex items-center justify-between p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
                <div>
                  <span class="font-bold">${escapeHtml(cf.name)}</span>
                  <span class="text-zinc-400 font-mono ml-1">(custom.${escapeHtml(cf.key)} • ${cf.field_type})</span>
                  ${cf.required ? '<span class="ml-1 text-rose-400 font-bold">Obrigatório</span>' : ''}
                </div>
                <button onclick="handleDeleteCustomField('${cf.id}')" class="text-rose-400 hover:underline">Excluir</button>
              </div>
            `).join('') || '<p class="text-xs text-zinc-400">Nenhum campo personalizado criado.</p>'}
          </div>
        </div>

        <!-- Custom Statuses Management -->
        <div class="space-y-4">
          <h4 class="text-sm font-extrabold text-pink-400">🏷️ Status Personalizados</h4>
          <form onsubmit="handleCreateStatus(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
            <div class="flex gap-2">
              <input id="st-name" type="text" required placeholder="Nome do Status (ex: Em Revisão)" class="flex-1 rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
              <input id="st-color" type="color" value="#a855f7" class="w-12 h-9 rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 p-1" />
              <input id="st-pos" type="number" value="25" placeholder="Ordem" class="w-20 rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-2 py-2 text-xs" />
            </div>
            <button type="submit" class="w-full py-2 rounded-xl bg-gradient-to-r from-pink-500 to-purple-600 text-white text-xs font-bold">+ Adicionar Status</button>
          </form>
          <div class="space-y-2">
            ${state.statuses.map((s) => `
              <div class="flex items-center justify-between p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 text-xs">
                <div class="flex items-center gap-2">
                  <span class="w-3 h-3 rounded-full" style="background:${escapeHtml(s.color)}"></span>
                  <span class="font-bold">${escapeHtml(s.name)}</span>
                  <span class="text-zinc-400 font-mono">(${escapeHtml(s.slug)})</span>
                </div>
                ${s.is_system ? '<span class="text-[10px] text-zinc-500">Sistema</span>' : `<button onclick="handleDeleteStatus('${s.id}')" class="text-rose-400">Excluir</button>`}
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  } else if (tab === 'projects') {
    container.innerHTML = `
      <div class="space-y-5">
        <form onsubmit="handleCreateProject(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex flex-wrap gap-2 items-center">
          <input id="proj-name" type="text" required placeholder="Nome do Projeto ou Categoria" class="flex-1 min-w-[180px] rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
          <input id="proj-desc" type="text" placeholder="Descrição..." class="flex-1 min-w-[180px] rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
          <input id="proj-color" type="color" value="#f97316" class="w-10 h-9 rounded-xl p-1 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800" />
          <button type="submit" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">+ Criar Projeto</button>
        </form>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          ${state.projects.map((p) => `
            <div class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
              <div class="flex items-center gap-3">
                <span class="w-4 h-4 rounded-full" style="background:${escapeHtml(p.color)}"></span>
                <div>
                  <p class="text-xs font-bold">${escapeHtml(p.name)}</p>
                  <p class="text-[11px] text-zinc-400">${escapeHtml(p.description || '')}</p>
                </div>
              </div>
              <button onclick="handleDeleteProject('${p.id}')" class="text-xs text-rose-400 hover:underline">Excluir</button>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  } else if (tab === 'users') {
    container.innerHTML = `
      <div class="space-y-5">
        <form onsubmit="handleCreateUser(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
          <h4 class="text-xs font-extrabold uppercase text-orange-400">Cadastrar Novo Usuário & Papel (RBAC)</h4>
          <div class="grid grid-cols-1 sm:grid-cols-5 gap-2">
            <input id="usr-username" type="text" required placeholder="Username" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <input id="usr-fullname" type="text" required placeholder="Nome Completo" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <input id="usr-email" type="email" required placeholder="E-mail" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <input id="usr-password" type="password" required placeholder="Senha" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <select id="usr-role" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs font-bold">
              <option value="member">Funcionário (Apenas atribuídas)</option>
              <option value="supervisor">Supervisor (Vê toda a equipe)</option>
              <option value="admin">Administrador Total</option>
              <option value="viewer">Visualizador (Somente leitura)</option>
            </select>
          </div>
          <button type="submit" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">+ Criar Usuário</button>
        </form>
        <div class="space-y-2">
          ${state.users.map((u) => `
            <div class="p-3 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex flex-wrap items-center justify-between gap-2 text-xs">
              <div>
                <span class="font-bold">${escapeHtml(u.full_name)}</span>
                <span class="text-zinc-400">(@${escapeHtml(u.username)} • ${escapeHtml(u.email)})</span>
                <span class="ml-2 px-2 py-0.5 rounded-full bg-pink-500/15 text-pink-400 font-bold uppercase text-[10px]">${escapeHtml(u.role)}</span>
              </div>
              ${u.id !== state.user.id ? `<button onclick="handleDeleteUser('${u.id}')" class="text-rose-400">Remover</button>` : '<span class="text-[10px] text-emerald-400">Você</span>'}
            </div>
          `).join('')}
        </div>
      </div>
    `;
  } else if (tab === 'webhooks') {
    const [whRes, delivRes] = await Promise.all([
      api('/api/v1/webhooks'),
      api('/api/v1/webhooks/deliveries?limit=25')
    ]);
    const eventsList = whRes.supported_events || [];
    container.innerHTML = `
      <div class="space-y-6">
        <form onsubmit="handleCreateWebhook(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
          <h4 class="text-xs font-extrabold uppercase text-pink-400">🪝 Cadastrar Novo Webhook de Saída</h4>
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <input id="wh-name" type="text" required placeholder="Nome (ex: n8n Workflow)" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <input id="wh-url" type="url" required placeholder="https://seu-n8n.com/webhook/..." class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <input id="wh-secret" type="text" placeholder="Segredo HMAC-SHA256 (opcional)" class="rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
          </div>
          <div>
            <span class="block text-[11px] font-bold uppercase text-zinc-400 mb-1">Eventos Disparadores:</span>
            <div class="flex flex-wrap gap-2">
              ${eventsList.map((ev) => `
                <label class="flex items-center gap-1.5 text-xs bg-white dark:bg-zinc-900 px-2.5 py-1 rounded-lg border border-zinc-200 dark:border-zinc-800">
                  <input type="checkbox" name="wh-events" value="${ev}" checked class="accent-pink-500" /> ${ev}
                </label>
              `).join('')}
            </div>
          </div>
          <button type="submit" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">+ Salvar Webhook</button>
        </form>

        <div class="space-y-2">
          <h5 class="text-xs font-bold uppercase text-zinc-400">Webhooks Configurados (${whRes.count})</h5>
          ${(whRes.items || []).map((w) => `
            <div class="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex flex-wrap items-center justify-between gap-2 text-xs">
              <div>
                <p class="font-bold">${escapeHtml(w.name)} — <code class="text-pink-400">${escapeHtml(w.target_url)}</code></p>
                <p class="text-[11px] text-zinc-400">Eventos: ${(w.events || []).join(', ')}</p>
              </div>
              <button onclick="handleDeleteWebhook('${w.id}')" class="text-rose-400">Excluir</button>
            </div>
          `).join('')}
        </div>

        <div class="space-y-2">
          <h5 class="text-xs font-bold uppercase text-zinc-400">Histórico de Envios & Respostas (${delivRes.count})</h5>
          <div class="max-h-56 overflow-y-auto space-y-1.5">
            ${(delivRes.items || []).map((d) => `
              <div class="p-2.5 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex items-center justify-between text-[11px]">
                <span><b>${escapeHtml(d.event_name)}</b> → ${escapeHtml(d.target_url)} (Tentativa #${d.attempt_count})</span>
                <span class="px-2 py-0.5 rounded-full font-bold ${d.status === 'success' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}">
                  ${escapeHtml(d.status)} (HTTP ${d.response_status || 0} • ${d.duration_ms}ms)
                </span>
              </div>
            `).join('') || '<p class="text-xs text-zinc-400">Nenhum disparo registrado ainda.</p>'}
          </div>
        </div>
      </div>
    `;
  } else if (tab === 'tokens') {
    const tokRes = await api('/api/v1/tokens');
    container.innerHTML = `
      <div class="space-y-6">
        <div class="p-4 rounded-2xl bg-gradient-to-r from-orange-500/10 via-pink-500/10 to-purple-500/10 border border-pink-500/30 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h4 class="text-sm font-extrabold">Documentação Interativa OpenAPI / Swagger & Observabilidade</h4>
            <p class="text-xs text-zinc-400">Integre facilmente com n8n, ChatGPT Actions, Codex, Hermes e agentes de IA.</p>
          </div>
          <div class="flex gap-2">
            <a href="/docs" target="_blank" class="px-4 py-2 rounded-xl bg-pink-500 text-white text-xs font-bold">Abrir Swagger (/docs) ↗</a>
            <a href="/executions?format=html&token=${encodeURIComponent(state.token)}" target="_blank" class="px-4 py-2 rounded-xl bg-zinc-800 text-zinc-100 text-xs font-bold">Logs (/executions) ↗</a>
          </div>
        </div>

        <form onsubmit="handleCreateApiToken(event)" class="flex gap-2">
          <input id="tok-name" type="text" required placeholder="Nome da Integração (ex: Agente n8n Produção)" class="flex-1 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
          <button type="submit" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">+ Gerar Token de API</button>
        </form>

        <div id="newly-created-token-box" class="hidden p-4 rounded-2xl bg-emerald-500/15 border border-emerald-500/40 text-xs space-y-1"></div>

        <div class="space-y-2">
          ${(tokRes.items || []).map((t) => `
            <div class="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex items-center justify-between text-xs">
              <div>
                <span class="font-bold">${escapeHtml(t.name)}</span>
                <code class="ml-2 text-orange-400">${escapeHtml(t.token_prefix)}...</code>
              </div>
              <button onclick="handleRevokeToken('${t.id}')" class="text-rose-400">Revogar</button>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  } else if (tab === 'backups') {
    const bkpRes = await api('/api/v1/backups');
    container.innerHTML = `
      <div class="space-y-5">
        <div class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h4 class="text-sm font-extrabold text-orange-400">💾 Backup Consistente Online (SQLite WAL + Anexos)</h4>
            <p class="text-xs text-zinc-400">Executa snapshot quente via API sqlite3.backup sem interromper a aplicação. Retenção: ${bkpRes.config.retention_count} arquivos.</p>
          </div>
          <button onclick="handleCreateBackupNow()" class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-orange-500 via-pink-500 to-purple-600 text-white text-xs font-bold shadow">
            ⚡ Criar Backup Agora
          </button>
        </div>

        <div class="space-y-2">
          ${(bkpRes.items || []).map((b) => `
            <div class="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex flex-wrap items-center justify-between gap-2 text-xs">
              <div>
                <p class="font-bold font-mono text-pink-400">${escapeHtml(b.filename)}</p>
                <p class="text-[11px] text-zinc-400">${escapeHtml(b.notes || '')} • ${(b.size_bytes / 1024).toFixed(1)} KB • ${escapeHtml(b.created_at)}</p>
              </div>
              <div class="flex items-center gap-2">
                <a href="/api/v1/backups/${b.id}/download?token=${encodeURIComponent(state.token)}" target="_blank" class="px-2.5 py-1 rounded-lg bg-zinc-200 dark:bg-zinc-800 font-semibold">Baixar .zip</a>
                <button onclick="handleRestoreBackup('${b.id}')" class="px-2.5 py-1 rounded-lg bg-amber-500/20 text-amber-300 font-semibold">Restaurar</button>
                <button onclick="handleDeleteBackup('${b.id}')" class="text-rose-400 px-2">Excluir</button>
              </div>
            </div>
          `).join('') || '<p class="text-xs text-zinc-400">Nenhum backup gerado até o momento.</p>'}
        </div>
      </div>
    `;
  } else if (tab === 'notifications') {
    const notifRes = await api('/api/v1/notifications?limit=30');
    const digTimes = (state.user && state.user.digest_times) ? state.user.digest_times.join(', ') : '08:00, 13:00, 18:00';
    container.innerHTML = `
      <div class="space-y-6">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
            <h4 class="text-sm font-extrabold text-pink-400">📲 Web Push VAPID (Android & Desktop)</h4>
            <p class="text-xs text-zinc-400">Receba alertas mesmo com o navegador fechado através do Service Worker.</p>
            <div class="flex flex-wrap gap-2">
              <button onclick="subscribeWebPushDevice()" class="px-4 py-2 rounded-xl bg-gradient-to-r from-orange-500 to-pink-500 text-white text-xs font-bold">
                Ativar Web Push neste Dispositivo
              </button>
              <button onclick="handleSendTestPush()" class="px-3 py-2 rounded-xl bg-zinc-200 dark:bg-zinc-800 text-xs font-semibold">
                Testar Notificação
              </button>
            </div>
          </div>

          <form onsubmit="handleSaveDigestPrefs(event)" class="p-4 rounded-2xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 space-y-3">
            <h4 class="text-sm font-extrabold text-orange-400">🕗 Resumos Automáticos (America/Sao_Paulo)</h4>
            <label class="flex items-center gap-2 text-xs">
              <input id="pref-digest-enabled" type="checkbox" ${state.user && state.user.digest_enabled ? 'checked' : ''} class="accent-pink-500" />
              Receber resumos automáticos de tarefas pendentes
            </label>
            <input id="pref-digest-times" type="text" value="${escapeHtml(digTimes)}" placeholder="08:00, 13:00, 18:00"
              class="w-full rounded-xl bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 px-3 py-2 text-xs" />
            <div class="flex gap-2">
              <button type="submit" class="px-4 py-1.5 rounded-xl bg-pink-500 text-white text-xs font-bold">Salvar Horários</button>
              <button type="button" onclick="handleTriggerDigestNow()" class="px-3 py-1.5 rounded-xl bg-zinc-200 dark:bg-zinc-800 text-xs font-semibold">Disparar Resumo Agora</button>
            </div>
          </form>
        </div>

        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <h5 class="text-xs font-bold uppercase text-zinc-400">Caixa de Entrada de Alertas (Atrasadas: ${notifRes.summary.overdue_count} | Hoje: ${notifRes.summary.today_count} | Futuras: ${notifRes.summary.upcoming_count})</h5>
            <button onclick="handleMarkAllRead()" class="text-xs text-pink-400 hover:underline">Marcar todas como lidas</button>
          </div>
          <div class="space-y-1.5 max-h-60 overflow-y-auto">
            ${(notifRes.items || []).map((n) => `
              <div class="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 flex items-center justify-between text-xs">
                <div>
                  <p class="font-bold">${escapeHtml(n.title)}</p>
                  <p class="text-zinc-400">${escapeHtml(n.body)}</p>
                </div>
                <span class="text-[10px] font-mono text-zinc-500">${escapeHtml((n.sent_at || '').slice(0, 16).replace('T', ' '))}</span>
              </div>
            `).join('') || '<p class="text-xs text-zinc-400">Nenhuma notificação registrada.</p>'}
          </div>
        </div>
      </div>
    `;
  }
}

// Admin Handlers
window.handleCreateCustomField = async function(e) {
  e.preventDefault();
  const optsRaw = document.getElementById('cf-options').value;
  const options = optsRaw ? optsRaw.split(',').map((s) => s.trim()).filter(Boolean) : [];
  await api('/api/v1/custom-fields', {
    method: 'POST',
    body: JSON.stringify({
      name: document.getElementById('cf-name').value,
      field_type: document.getElementById('cf-type').value,
      options,
      required: document.getElementById('cf-req').checked
    })
  });
  showToast('Campo personalizado criado!', 'success');
  await loadMetadata();
  await renderAdminTabContent('fields');
};

window.handleDeleteCustomField = async function(id) {
  await api(`/api/v1/custom-fields/${id}`, { method: 'DELETE' });
  await loadMetadata();
  await renderAdminTabContent('fields');
};

window.handleCreateStatus = async function(e) {
  e.preventDefault();
  await api('/api/v1/statuses', {
    method: 'POST',
    body: JSON.stringify({
      name: document.getElementById('st-name').value,
      color: document.getElementById('st-color').value,
      position: parseInt(document.getElementById('st-pos').value || '50', 10)
    })
  });
  showToast('Status personalizado criado!', 'success');
  await loadMetadata();
  await renderAdminTabContent('fields');
};

window.handleDeleteStatus = async function(id) {
  await api(`/api/v1/statuses/${id}`, { method: 'DELETE' });
  await loadMetadata();
  await renderAdminTabContent('fields');
};

window.handleCreateProject = async function(e) {
  e.preventDefault();
  await api('/api/v1/projects', {
    method: 'POST',
    body: JSON.stringify({
      name: document.getElementById('proj-name').value,
      description: document.getElementById('proj-desc').value,
      color: document.getElementById('proj-color').value
    })
  });
  showToast('Projeto criado!', 'success');
  await loadMetadata();
  await renderAdminTabContent('projects');
};

window.handleDeleteProject = async function(id) {
  await api(`/api/v1/projects/${id}`, { method: 'DELETE' });
  await loadMetadata();
  await renderAdminTabContent('projects');
};

window.handleCreateUser = async function(e) {
  e.preventDefault();
  await api('/api/v1/users', {
    method: 'POST',
    body: JSON.stringify({
      username: document.getElementById('usr-username').value,
      full_name: document.getElementById('usr-fullname').value,
      email: document.getElementById('usr-email').value,
      password: document.getElementById('usr-password').value,
      role: document.getElementById('usr-role').value
    })
  });
  showToast('Usuário criado!', 'success');
  await loadMetadata();
  await renderAdminTabContent('users');
};

window.handleDeleteUser = async function(id) {
  await api(`/api/v1/users/${id}`, { method: 'DELETE' });
  await loadMetadata();
  await renderAdminTabContent('users');
};

window.handleCreateWebhook = async function(e) {
  e.preventDefault();
  const checked = Array.from(document.querySelectorAll('input[name="wh-events"]:checked')).map((el) => el.value);
  await api('/api/v1/webhooks', {
    method: 'POST',
    body: JSON.stringify({
      name: document.getElementById('wh-name').value,
      target_url: document.getElementById('wh-url').value,
      secret: document.getElementById('wh-secret').value,
      events: checked
    })
  });
  showToast('Webhook cadastrado!', 'success');
  await renderAdminTabContent('webhooks');
};

window.handleDeleteWebhook = async function(id) {
  await api(`/api/v1/webhooks/${id}`, { method: 'DELETE' });
  await renderAdminTabContent('webhooks');
};

window.handleCreateApiToken = async function(e) {
  e.preventDefault();
  const res = await api('/api/v1/tokens', {
    method: 'POST',
    body: JSON.stringify({ name: document.getElementById('tok-name').value })
  });
  await renderAdminTabContent('tokens');
  const box = document.getElementById('newly-created-token-box');
  if (box) {
    box.classList.remove('hidden');
    box.innerHTML = `<p class="font-bold text-emerald-300">Copie seu Token de API agora (exibido apenas uma vez):</p><code class="block p-2 rounded bg-zinc-950 text-emerald-400 font-mono select-all">${escapeHtml(res.token)}</code>`;
  }
};

window.handleRevokeToken = async function(id) {
  await api(`/api/v1/tokens/${id}`, { method: 'DELETE' });
  await renderAdminTabContent('tokens');
};

window.handleCreateBackupNow = async function() {
  await api('/api/v1/backups', {
    method: 'POST',
    body: JSON.stringify({ notes: 'Backup manual pelo painel', include_attachments: true })
  });
  showToast('Backup consistente criado com sucesso!', 'success');
  await renderAdminTabContent('backups');
};

window.handleRestoreBackup = async function(id) {
  await api(`/api/v1/backups/${id}/restore`, { method: 'POST' });
  showToast('Backup restaurado online com sucesso!', 'success');
  await loadMetadata();
  await fetchTasks();
};

window.handleDeleteBackup = async function(id) {
  await api(`/api/v1/backups/${id}`, { method: 'DELETE' });
  await renderAdminTabContent('backups');
};

window.handleSendTestPush = async function() {
  await api('/api/v1/push/test', { method: 'POST' });
  showToast('Notificação de teste disparada!', 'success');
  await renderAdminTabContent('notifications');
  await refreshNotificationBadge();
};

window.handleSaveDigestPrefs = async function(e) {
  e.preventDefault();
  const enabled = document.getElementById('pref-digest-enabled').checked;
  const times = document.getElementById('pref-digest-times').value.split(',').map((s) => s.trim()).filter(Boolean);
  const res = await api('/api/v1/auth/me', {
    method: 'PATCH',
    body: JSON.stringify({ digest_enabled: enabled, digest_times: times })
  });
  state.user = res.user;
  showToast('Horários de resumo salvos!', 'success');
};

window.handleTriggerDigestNow = async function() {
  await api('/api/v1/notifications/trigger-digest', { method: 'POST' });
  showToast('Resumo de pendentes gerado!', 'success');
  await renderAdminTabContent('notifications');
  await refreshNotificationBadge();
};

window.handleMarkAllRead = async function() {
  await api('/api/v1/notifications/read-all', { method: 'POST' });
  await refreshNotificationBadge();
  await renderAdminTabContent('notifications');
};

// =====================================================================
// DOM EVENT LISTENERS
// =====================================================================
function closeMobileSidebar() {
  document.getElementById('sidebar').classList.add('-translate-x-full');
}

document.addEventListener('DOMContentLoaded', () => {
  bootstrapApp();

  // Login Form
  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('login-error');
    errEl.classList.add('hidden');
    try {
      const res = await api('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({
          username: document.getElementById('login-username').value,
          password: document.getElementById('login-password').value
        })
      });
      state.token = res.access_token;
      localStorage.setItem('tf_token', res.access_token);
      await bootstrapApp();
      showToast('Bem-vindo ao TaskForge!', 'success');
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
    }
  });

  // Theme & Logout
  document.getElementById('btn-theme-toggle').addEventListener('click', toggleTheme);
  document.getElementById('btn-logout').addEventListener('click', handleLogout);

  // Sidebar Mobile Drawer
  document.getElementById('btn-open-sidebar').addEventListener('click', () => {
    document.getElementById('sidebar').classList.remove('-translate-x-full');
  });
  document.getElementById('btn-close-sidebar').addEventListener('click', closeMobileSidebar);

  // Mobile Bottom Nav
  document.getElementById('mob-nav-views').addEventListener('click', () => {
    document.getElementById('sidebar').classList.remove('-translate-x-full');
  });
  document.getElementById('mob-nav-filters').addEventListener('click', () => {
    document.getElementById('filter-builder-panel').classList.toggle('hidden');
  });
  document.getElementById('mob-nav-new-task').addEventListener('click', () => openTaskModal(null));
  document.getElementById('mob-nav-notifs').addEventListener('click', () => openAdminModal('notifications'));
  document.getElementById('mob-nav-admin').addEventListener('click', () => openAdminModal('fields'));

  // Clear Multi-Project Filter
  document.getElementById('btn-clear-projects').addEventListener('click', () => {
    state.selectedProjects = [];
    renderSidebarProjects();
    fetchTasks();
  });

  // View Mode Switchers (Table / Kanban / List)
  document.querySelectorAll('.btn-view-mode').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.viewMode = btn.getAttribute('data-view-mode');
      updateViewModeButtons();
      renderTasksViewport();
    });
  });

  // Global Search (Debounced)
  let searchTimer = null;
  document.getElementById('global-search-input').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.searchQuery = e.target.value;
      state.page = 1;
      fetchTasks();
    }, 220);
  });

  // Bento Quick Filter Cards
  document.querySelectorAll('.bento-card').forEach((card) => {
    card.addEventListener('click', () => {
      const preset = card.getAttribute('data-quick-preset');
      if (preset === 'all') {
        state.filterTree = { logic: 'AND', conditions: [], groups: [] };
      } else if (preset === 'overdue') {
        state.filterTree = {
          logic: 'AND',
          conditions: [
            { field: 'due_date', op: 'is_overdue', value: null },
            { field: 'status_slug', op: 'not_in', value: ['completed', 'cancelled'] }
          ],
          groups: []
        };
      } else if (preset === 'today') {
        state.filterTree = {
          logic: 'AND',
          conditions: [
            { field: 'due_date', op: 'is_today', value: null },
            { field: 'status_slug', op: 'not_in', value: ['completed', 'cancelled'] }
          ],
          groups: []
        };
      } else if (preset === 'completed') {
        state.filterTree = {
          logic: 'AND',
          conditions: [{ field: 'status_slug', op: 'eq', value: 'completed' }],
          groups: []
        };
      }
      renderFilterBuilder();
      fetchTasks();
    });
  });

  // Quick 1-Tap Task Add Form
  document.getElementById('quick-add-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const titleInput = document.getElementById('quick-task-title');
    const title = titleInput.value.trim();
    if (!title) return;
    try {
      await api('/api/v1/tasks', {
        method: 'POST',
        body: JSON.stringify({
          title,
          priority: document.getElementById('quick-task-priority').value,
          project_id: document.getElementById('quick-task-project').value || null,
          due_date: document.getElementById('quick-task-due').value || null
        })
      });
      titleInput.value = '';
      showToast('Tarefa adicionada!', 'success');
      await fetchTasks();
    } catch (err) {
      showToast(err.message, 'error');
    }
  });

  // Filter Builder Controls
  document.getElementById('btn-toggle-filter-panel').addEventListener('click', () => {
    document.getElementById('filter-builder-panel').classList.toggle('hidden');
  });
  document.getElementById('filter-root-logic').addEventListener('change', (e) => {
    state.filterTree.logic = e.target.value;
  });
  document.getElementById('btn-add-filter-cond').addEventListener('click', () => {
    state.filterTree.conditions.push({ field: 'priority', op: 'eq', value: 'P1' });
    renderFilterBuilder();
  });
  document.getElementById('btn-add-filter-group').addEventListener('click', () => {
    state.filterTree.groups.push({
      logic: 'OR',
      conditions: [{ field: 'status_slug', op: 'eq', value: 'pending' }],
      groups: []
    });
    renderFilterBuilder();
  });
  document.getElementById('btn-reset-filters').addEventListener('click', () => {
    state.filterTree = { logic: 'AND', conditions: [], groups: [] };
    state.groupBy = '';
    document.getElementById('view-group-by').value = '';
    renderFilterBuilder();
    fetchTasks();
  });
  document.getElementById('btn-apply-filters').addEventListener('click', () => {
    state.groupBy = document.getElementById('view-group-by').value;
    state.sorts = [{
      field: document.getElementById('view-sort-field').value,
      dir: document.getElementById('view-sort-dir').value
    }];
    state.page = 1;
    fetchTasks();
  });

  // Save / Create / Duplicate / Delete Custom Views
  document.getElementById('btn-save-current-view').addEventListener('click', async () => {
    if (!state.activeView) return;
    const payload = {
      name: state.activeView.name,
      view_type: state.viewMode,
      visible_columns: state.visibleColumns,
      column_order: state.columnOrder,
      filters: state.filterTree,
      sorts: state.sorts,
      group_by: document.getElementById('view-group-by').value || '',
      is_default: state.activeView.is_default,
      is_shared: state.activeView.is_shared
    };
    await api(`/api/v1/views/${state.activeView.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
    showToast('Visualização atualizada!', 'success');
    await loadMetadata();
  });

  const createNewViewHandler = async () => {
    const name = prompt('Nome da nova visualização personalizada:');
    if (!name || !name.trim()) return;
    const isShared = confirm('Compartilhar esta visualização com outros usuários autorizados? (OK = Compartilhada, Cancelar = Privada)');
    const isDefault = confirm('Definir como visualização padrão inicial?');
    const res = await api('/api/v1/views', {
      method: 'POST',
      body: JSON.stringify({
        name: name.trim(),
        view_type: state.viewMode,
        visible_columns: state.visibleColumns,
        column_order: state.columnOrder,
        filters: state.filterTree,
        sorts: state.sorts,
        group_by: document.getElementById('view-group-by').value || '',
        is_default: isDefault,
        is_shared: isShared
      })
    });
    showToast('Nova visualização salva!', 'success');
    await loadMetadata();
    applySavedView(res.view, true);
  };
  document.getElementById('btn-save-as-new-view').addEventListener('click', createNewViewHandler);
  document.getElementById('btn-create-view').addEventListener('click', createNewViewHandler);

  document.getElementById('btn-duplicate-view').addEventListener('click', async () => {
    if (!state.activeView) return;
    const res = await api(`/api/v1/views/${state.activeView.id}/duplicate`, { method: 'POST' });
    showToast('Visualização duplicada!', 'success');
    await loadMetadata();
    applySavedView(res.view, true);
  });

  document.getElementById('btn-delete-view').addEventListener('click', async () => {
    if (!state.activeView) return;
    await api(`/api/v1/views/${state.activeView.id}`, { method: 'DELETE' });
    showToast('Visualização excluída!', 'info');
    state.activeView = null;
    await loadMetadata();
    selectInitialView();
    await fetchTasks();
  });

  // Pagination Buttons
  document.getElementById('btn-prev-page').addEventListener('click', () => {
    if (state.page > 1) {
      state.page--;
      fetchTasks();
    }
  });
  document.getElementById('btn-next-page').addEventListener('click', () => {
    if (state.page < state.totalPages) {
      state.page++;
      fetchTasks();
    }
  });

  // Task Modal Controls
  document.getElementById('btn-new-task-sidebar').addEventListener('click', () => openTaskModal(null));
  document.getElementById('btn-close-task-modal').addEventListener('click', () => {
    document.getElementById('task-modal').classList.add('hidden');
    state.activeTaskModalId = null;
  });
  document.getElementById('btn-clear-due-date').addEventListener('click', () => {
    document.getElementById('form-task-due-date').value = '';
    document.getElementById('form-task-due-time').value = '';
  });

  document.getElementById('task-detail-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const taskId = document.getElementById('form-task-id').value;
    const recFreq = document.getElementById('form-task-recurrence').value;
    const partSel = document.getElementById('form-task-participants');
    const participants = Array.from(partSel.selectedOptions).map((o) => o.value);
    const tags = document.getElementById('form-task-tags').value
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    const customValues = {};
    document.querySelectorAll('[data-cf-key]').forEach((el) => {
      const k = el.getAttribute('data-cf-key');
      if (el.type === 'checkbox') {
        customValues[k] = el.checked;
      } else {
        customValues[k] = el.value;
      }
    });

    const payload = {
      title: document.getElementById('form-task-title').value,
      color: document.getElementById('form-task-color').value,
      status_slug: document.getElementById('form-task-status').value,
      priority: document.getElementById('form-task-priority').value,
      project_id: document.getElementById('form-task-project').value || null,
      assignee_id: document.getElementById('form-task-assignee').value || null,
      due_date: document.getElementById('form-task-due-date').value || null,
      due_time: document.getElementById('form-task-due-time').value || null,
      recurrence: recFreq ? { enabled: true, frequency: recFreq, interval: 1 } : null,
      tags,
      participants,
      description: document.getElementById('form-task-description').value,
      notes: document.getElementById('form-task-notes').value,
      custom_values: customValues
    };

    try {
      if (taskId) {
        await api(`/api/v1/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        showToast('Tarefa atualizada!', 'success');
      } else {
        await api('/api/v1/tasks', { method: 'POST', body: JSON.stringify(payload) });
        showToast('Tarefa criada!', 'success');
      }
      document.getElementById('task-modal').classList.add('hidden');
      await fetchTasks();
    } catch (err) {
      showToast(err.message, 'error');
    }
  });

  document.getElementById('btn-delete-task-modal').addEventListener('click', async () => {
    const taskId = document.getElementById('form-task-id').value;
    if (!taskId) return;
    await api(`/api/v1/tasks/${taskId}`, { method: 'DELETE' });
    document.getElementById('task-modal').classList.add('hidden');
    showToast('Tarefa excluída!', 'info');
    await fetchTasks();
  });

  // Subtask Add in Modal
  document.getElementById('btn-add-subtask').addEventListener('click', async () => {
    const taskId = document.getElementById('form-task-id').value;
    const inp = document.getElementById('new-subtask-input');
    if (!taskId || !inp.value.trim()) return;
    await api(`/api/v1/tasks/${taskId}/subtasks`, {
      method: 'POST',
      body: JSON.stringify({ title: inp.value.trim() })
    });
    inp.value = '';
    await openTaskModal(taskId, false);
    await fetchTasks();
  });

  // Reminder Add in Modal
  document.getElementById('btn-add-reminder').addEventListener('click', async () => {
    const taskId = document.getElementById('form-task-id').value;
    const dt = document.getElementById('new-reminder-dt').value;
    const msg = document.getElementById('new-reminder-msg').value;
    if (!taskId || !dt) return;
    await api('/api/v1/reminders', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId, remind_at: dt, message: msg })
    });
    document.getElementById('new-reminder-msg').value = '';
    showToast('Lembrete agendado!', 'success');
    await openTaskModal(taskId, false);
  });

  // Attachment Upload in Modal
  document.getElementById('btn-upload-attachment').addEventListener('click', async () => {
    const taskId = document.getElementById('form-task-id').value;
    const fileInput = document.getElementById('new-attachment-file');
    if (!taskId || !fileInput.files.length) return;
    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    await api(`/api/v1/tasks/${taskId}/attachments`, { method: 'POST', body: fd });
    fileInput.value = '';
    showToast('Anexo enviado!', 'success');
    await openTaskModal(taskId, false);
    await fetchTasks();
  });

  // Comment Add in Modal
  document.getElementById('btn-add-comment').addEventListener('click', async () => {
    const taskId = document.getElementById('form-task-id').value;
    const inp = document.getElementById('new-comment-input');
    if (!taskId || !inp.value.trim()) return;
    await api(`/api/v1/tasks/${taskId}/comments`, {
      method: 'POST',
      body: JSON.stringify({ content: inp.value.trim() })
    });
    inp.value = '';
    await openTaskModal(taskId, false);
    await fetchTasks();
  });

  // Admin Modal Open / Close / Tabs
  document.querySelectorAll('.btn-open-admin').forEach((btn) => {
    btn.addEventListener('click', () => {
      closeMobileSidebar();
      openAdminModal(btn.getAttribute('data-admin-tab'));
    });
  });
  document.getElementById('btn-open-notifications').addEventListener('click', () => openAdminModal('notifications'));
  document.getElementById('btn-close-admin-modal').addEventListener('click', () => {
    document.getElementById('admin-modal').classList.add('hidden');
  });
  document.querySelectorAll('.admin-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => openAdminModal(btn.getAttribute('data-tab')));
  });

  // Install PWA Button
  document.getElementById('btn-install-pwa').addEventListener('click', async () => {
    if (!state.deferredPwaPrompt) return;
    state.deferredPwaPrompt.prompt();
    await state.deferredPwaPrompt.userChoice;
    state.deferredPwaPrompt = null;
    document.getElementById('btn-install-pwa').classList.add('hidden');
  });
});
