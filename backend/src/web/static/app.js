/* 计量复判班报前端：原生 JS，无构建依赖。 */
'use strict';

const api = {
  token: localStorage.getItem('rejudge_token') || '',
  async request(method, path, body) {
    const headers = {};
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const resp = await fetch(path, {
      method, headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  },
  get(path) { return this.request('GET', path); },
  post(path, body) { return this.request('POST', path, body); },
};

const state = { user: null, batches: [], selectedBatch: null };

const $ = (sel) => document.querySelector(sel);
const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;');

const STATUS_LABEL = {
  current: '当前有效', pending: '待核/待重算', superseded: '已取代（留档）',
  running: '计算中', failed: '失败', ok: '已覆盖',
};

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 3200);
}

function badge(status) {
  return `<span class="badge badge-${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

// ------------------------------------------------------------- 登录会话
async function login(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const errorBox = $('#login-error');
  errorBox.classList.add('hidden');
  try {
    const data = await api.post('/api/login', {
      username: form.get('username'), password: form.get('password'),
    });
    api.token = data.token;
    localStorage.setItem('rejudge_token', data.token);
    state.user = data.user;
    enterWorkspace();
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.className = 'alert error';
  }
}

async function logout() {
  try { await api.post('/api/logout', {}); } catch { /* 忽略 */ }
  localStorage.removeItem('rejudge_token');
  api.token = '';
  state.user = null;
  location.reload();
}

function applyRole() {
  const role = state.user?.role;
  document.querySelectorAll('.writer-only, .admin-only').forEach((el) => el.classList.add('hidden'));
  if (role === 'admin' || role === 'analyst') {
    document.querySelectorAll('.writer-only').forEach((el) => el.classList.remove('hidden'));
  }
  if (role === 'admin') {
    document.querySelectorAll('.admin-only').forEach((el) => el.classList.remove('hidden'));
  }
}

function enterWorkspace() {
  $('#view-login').classList.add('hidden');
  $('#workspace').classList.remove('hidden');
  $('#session-box').classList.remove('hidden');
  $('#session-name').textContent = `${state.user.name}（${state.user.role}）`;
  applyRole();
  switchTab('report');
  loadBatches();
}

// ------------------------------------------------------------- 页签
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) =>
    p.classList.toggle('hidden', p.id !== `tab-${name}`));
  if (name === 'report') loadBatches();
  if (name === 'history') loadRuns();
  if (name === 'audit') loadAudit();
  if (name === 'import') loadCorrections();
}

// ------------------------------------------------------------- 班报总览
async function loadBatches() {
  const data = await api.get('/api/batches');
  state.batches = data.batches;
  const wrap = $('#batch-cards');
  if (!state.batches.length) {
    wrap.innerHTML = '<div class="card">暂无批次，请先在「导入与更正」中上传读数 CSV。</div>';
    return;
  }
  wrap.innerHTML = state.batches.map((b) => {
    const run = b.latest_run || {};
    const status = run.status || 'pending';
    return `
    <article class="batch-card is-${esc(status)}" data-batch="${b.id}">
      <h3><span>${esc(b.code)}</span>${badge(status)}</h3>
      <div class="batch-meta">
        ${esc(b.product)} · 规格 [${esc(b.spec_lower)}, ${esc(b.spec_upper)}]<br>
        ${b.piece_count} 件 · ${b.tool_count} 台器具 · 来源 ${esc(b.source_name)}
      </div>
      <div class="batch-stat">
        <span class="mini">运行 #${run.id ?? '—'}</span>
        <span class="mini">${esc(run.detail || '等待首次复判')}</span>
      </div>
    </article>`;
  }).join('');
  wrap.querySelectorAll('.batch-card').forEach((card) =>
    card.addEventListener('click', () => openBatch(Number(card.dataset.batch))));
  if (state.selectedBatch) renderDetail(state.selectedBatch);
}

async function openBatch(batchId) {
  state.selectedBatch = batchId;
  await renderDetail(batchId);
}

async function renderDetail(batchId) {
  const detail = await api.get(`/api/batches/${batchId}/detail`);
  const { batch, latest_run: run, versions, pieces } = detail;
  const canWrite = state.user.role === 'admin' || state.user.role === 'analyst';
  const pendingGroups = versions.filter((v) => v.status === 'pending');

  const groupsHtml = versions.map((v) => {
    if (v.status === 'pending') {
      return `<div class="group-block">
        <div class="group-head">
          <strong>器具 ${esc(v.tool_id)}</strong>${badge('pending')}
        </div>
        <p class="hint" style="margin-top:8px">该时段（组内 ${v.group_piece_count} 件）缺少修正范围，待校准记录补入后重算。</p>
      </div>`;
    }
    return `<div class="group-block">
      <div class="group-head">
        <strong>器具 ${esc(v.tool_id)} · ${esc(v.period_start)} ~ ${esc(v.period_end)}</strong>
        ${badge('ok')}<span class="mini">组内 ${v.group_piece_count} 件 · 修正范围 [${esc(v.range_lower)}, ${esc(v.range_upper)}]</span>
      </div>
      <div class="extremum-grid">
        <div class="extremum min">
          <div class="mini">可同时合格 · 最少件数</div>
          <div class="num">${v.min_count}<span class="mini"> / ${v.group_piece_count}</span></div>
          <div class="mini">达到该件数的修正量取值 <span class="witness">c = ${esc(v.min_witness)}</span></div>
        </div>
        <div class="extremum max">
          <div class="mini">可同时合格 · 最多件数</div>
          <div class="num">${v.max_count}<span class="mini"> / ${v.group_piece_count}</span></div>
          <div class="mini">达到该件数的修正量取值 <span class="witness">c = ${esc(v.max_witness)}</span></div>
        </div>
      </div>
    </div>`;
  }).join('');

  $('#batch-detail').classList.remove('hidden');
  $('#batch-detail').innerHTML = `
    <div class="panel-head">
      <h2>${esc(batch.code)} · ${esc(batch.product)}</h2>
      <div class="row" style="margin:0">
        ${canWrite ? `<button class="btn btn-primary btn-sm" id="btn-recompute" type="button">重算该批次</button>` : ''}
        <button class="btn btn-ghost btn-sm" id="btn-close-detail" type="button">收起</button>
      </div>
    </div>
    <div class="batch-meta">
      规格区间 [${esc(batch.spec_lower)}, ${esc(batch.spec_upper)}] · 数据指纹 <code>${esc(batch.fingerprint.slice(0, 16))}…</code><br>
      最新运行 #${run.id}：${badge(run.status)} ${esc(run.detail || '')}
    </div>
    ${pendingGroups.length ? `<div class="pending-note">有 ${pendingGroups.length} 个器具时段缺修正范围，批次结论为待核；补入迟到记录后请重算。</div>` : ''}
    ${groupsHtml}
    <h3 style="margin-top:18px">逐件读数与测量时段</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>序号</th><th>零件编号</th><th>读数</th><th>器具</th><th>测量时间</th></tr></thead>
        <tbody>${pieces.map((p) => `<tr>
          <td>${p.seq}</td><td>${esc(p.piece_no)}</td><td>${esc(p.reading)}</td>
          <td>${esc(p.tool_id)}</td><td>${esc(p.measured_at)}</td></tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  $('#btn-close-detail').addEventListener('click', () => {
    state.selectedBatch = null;
    $('#batch-detail').classList.add('hidden');
  });
  const recomputeBtn = $('#btn-recompute');
  if (recomputeBtn) recomputeBtn.addEventListener('click', () => recompute(batch.id));
}

async function recompute(batchId) {
  const btn = $('#btn-recompute');
  btn.disabled = true;
  try {
    await api.post(`/api/batches/${batchId}/recompute`, {});
    toast('复判完成，结果已更新', 'success');
    await loadBatches();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------- 导入与更正
function bindFileLoader(inputId, textareaId) {
  $(`#${inputId}`).addEventListener('change', async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $(`#${textareaId}`).value = await file.text();
  });
}

function feedback(message, kind) {
  const el = $('#import-feedback');
  el.textContent = message;
  el.className = `alert ${kind}`;
}

async function importReadings() {
  const content = $('#csv-readings').value;
  if (!content.trim()) return feedback('请先粘贴或选择读数 CSV。', 'error');
  try {
    const data = await api.post('/api/import/readings', { filename: 'readings.csv', content });
    feedback(`读数批次已导入（#${data.batch_id}），当前处于待核状态。`, 'success');
    $('#csv-readings').value = '';
    toast('读数导入成功', 'success');
  } catch (err) {
    feedback(err.message, 'error');
  }
}

async function importCorrections() {
  const content = $('#csv-corrections').value;
  if (!content.trim()) return feedback('请先粘贴或选择修正范围 CSV。', 'error');
  try {
    const data = await api.post('/api/import/corrections', { filename: 'corrections.csv', content });
    const affected = data.affected_batch_ids.length
      ? `，受影响批次 ${data.affected_batch_ids.join('、')} 已置为待重算` : '';
    feedback(`补录 ${data.inserted} 条、更正 ${data.corrected} 条${affected}。`, 'success');
    $('#csv-corrections').value = '';
    toast('修正记录已处理', 'success');
    loadCorrections();
  } catch (err) {
    feedback(err.message, 'error');
  }
}

async function loadCorrections() {
  const data = await api.get('/api/corrections');
  const rows = data.corrections;
  $('#correction-table-wrap').innerHTML = rows.length ? `
    <h3 style="margin-top:16px">现行与历史修正范围（${rows.length} 条）</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>器具</th><th>时段</th><th>范围</th><th>版本</th><th>状态</th><th>来源</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.tool_id)}</td>
        <td class="wrap">${esc(r.period_start)}<br>${esc(r.period_end)}</td>
        <td>[${esc(r.range_lower)}, ${esc(r.range_upper)}]</td>
        <td>v${r.version}</td>
        <td>${r.superseded_at ? badge('superseded') : badge('current')}</td>
        <td>${esc(r.source_name)}</td></tr>`).join('')}
      </tbody></table></div>` : '<p class="hint">尚无修正范围记录。</p>';
}

// ------------------------------------------------------------- 运行留痕
async function loadRuns() {
  const data = await api.get('/api/runs');
  const tbody = document.querySelector('#runs-table tbody');
  tbody.innerHTML = data.runs.map((r) => `<tr>
    <td>${r.id}</td><td>${esc(r.batch_code)}</td>
    <td>${esc(r.started_at)}</td><td>${esc(r.finished_at || '—')}</td>
    <td>${badge(r.status)}</td><td>${esc(r.reason)}</td>
    <td class="wrap">${esc(r.detail || '')}</td></tr>`).join('')
    || '<tr><td colspan="7" class="hint">暂无运行记录。</td></tr>';
}

// ------------------------------------------------------------- 审计
async function loadAudit() {
  const data = await api.get('/api/audit');
  const tbody = document.querySelector('#audit-table tbody');
  tbody.innerHTML = data.events.map((e) => `<tr>
    <td>${esc(e.at)}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td>
    <td>${esc(e.target || '')}</td><td class="wrap">${esc(e.detail || '')}</td></tr>`).join('')
    || '<tr><td colspan="5" class="hint">暂无审计事件。</td></tr>';
}

// ------------------------------------------------------------- 启动
async function boot() {
  $('#form-login').addEventListener('submit', login);
  $('#btn-logout').addEventListener('click', logout);
  document.querySelectorAll('.tab').forEach((t) =>
    t.addEventListener('click', () => switchTab(t.dataset.tab)));
  bindFileLoader('file-readings', 'csv-readings');
  bindFileLoader('file-corrections', 'csv-corrections');
  $('#btn-import-readings').addEventListener('click', importReadings);
  $('#btn-import-corrections').addEventListener('click', importCorrections);
  $('#btn-refresh').addEventListener('click', loadBatches);

  if (api.token) {
    try {
      const me = await api.get('/api/me');
      state.user = me.user;
      enterWorkspace();
      return;
    } catch {
      localStorage.removeItem('rejudge_token');
      api.token = '';
    }
  }
  $('#view-login').classList.remove('hidden');
}

boot();
