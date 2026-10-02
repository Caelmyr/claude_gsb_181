/* 结果对比 Compare — 按键对齐两个作业的结果并标出差异 */
Components.init('compare');
const C = Components;

let jobs = [];
let jobA = '';
let jobB = '';
let typeFilter = '';
let query = '';
let offset = 0;
const LIMIT = 100;
let lastTotal = 0;
let lastSummary = null;
let searchTimer = null;

const TYPE_LABEL = {
  only_in_a: '仅 A Only A',
  only_in_b: '仅 B Only B',
  value_mismatch: '值不同 Different',
};
const TYPE_BADGE = { only_in_a: 'warn', only_in_b: 'aqua', value_mismatch: 'bad' };
const TYPE_ROW = { only_in_a: 'diff-only-a', only_in_b: 'diff-only-b', value_mismatch: 'diff-value' };

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------
function fmtVal(v) {
  if (v === null || v === undefined) return '<span class="muted">null</span>';
  if (typeof v === 'number') return `<span class="tabular">${C.esc(Number(v.toPrecision(12)))}</span>`;
  if (typeof v === 'object') {
    const s = JSON.stringify(v);
    return `<span class="mono small">${C.esc(s.length > 80 ? s.slice(0, 80) + '…' : s)}</span>`;
  }
  return C.esc(v);
}

function fmtDelta(d) {
  if (d === null || d === undefined) return '';
  const n = Number(d.toPrecision(6));
  return `<span class="delta">Δ ${n > 0 ? '+' : ''}${C.esc(n)}</span>`;
}

// Render the value half of a result record (everything except "key").
function recCell(rec) {
  if (rec === null || rec === undefined) return '<span class="muted">缺失 —</span>';
  if (typeof rec === 'object' && !Array.isArray(rec)) return C.valueCell(rec);
  return fmtVal(rec);
}

// Field-level detail for a value mismatch; duplicate info otherwise.
function detailCell(d) {
  if (d.duplicate) {
    return `<span class="badge warn">标识重复 Duplicate</span> ` +
      `<span class="small muted">A 侧 ${d.count_a} 条 / B 侧 ${d.count_b} 条，多重集合不一致 multisets differ</span>`;
  }
  if (!d.fields || !d.fields.length) return '<span class="muted">-</span>';
  const changed = d.fields.filter(f => !f.same);
  const sameN = d.fields.length - changed.length;
  const rows = changed.map(f => {
    const va = f.missing === 'a' ? '<span class="muted">缺失 missing</span>' : fmtVal(f.a);
    const vb = f.missing === 'b' ? '<span class="muted">缺失 missing</span>' : fmtVal(f.b);
    return `<div class="field-diff"><span class="mono">${C.esc(f.field)}</span>: ${va} → ${vb} ${fmtDelta(f.delta)}</div>`;
  }).join('');
  const note = sameN ? `<div class="muted small">其余 ${sameN} 个字段一致 ${sameN} field(s) match</div>` : '';
  return rows + note;
}

// ---------------------------------------------------------------------------
// Job pickers (two selects sharing one job list)
// ---------------------------------------------------------------------------
function jobOptions(selected) {
  return '<option value="">选择作业 Select job…</option>' + jobs.map(j =>
    `<option value="${j.job_id}"${j.job_id === selected ? ' selected' : ''}>` +
    `${C.esc(j.name)} — ${j.status}</option>`).join('');
}

function buildPickers() {
  const done = jobs.filter(j => j.status === 'SUCCEEDED');
  if (!jobA) jobA = (done[0] || jobs[0] || {}).job_id || '';
  if (!jobB) jobB = (done[1] || done[0] || jobs[1] || jobs[0] || {}).job_id || '';
  document.getElementById('picker-a').innerHTML = `<select class="job-select" id="sel-a">${jobOptions(jobA)}</select>`;
  document.getElementById('picker-b').innerHTML = `<select class="job-select" id="sel-b">${jobOptions(jobB)}</select>`;
  document.getElementById('sel-a').addEventListener('change', e => { jobA = e.target.value; resetAndRun(); });
  document.getElementById('sel-b').addEventListener('change', e => { jobB = e.target.value; resetAndRun(); });
}

async function loadJobs() {
  let d;
  try { d = await API.get('/api/jobs'); } catch (e) {
    document.getElementById('picker-a').innerHTML = C.empty('无法连接 Master (Cannot reach master)');
    return;
  }
  jobs = d.jobs || [];
  buildPickers();
  runCompare();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function renderWarnings(warnings) {
  document.getElementById('warnings').innerHTML = (warnings || []).map(w =>
    `<div class="banner warn">${C.esc(w)}</div>`).join('');
}

function renderStats(d) {
  const s = d.summary;
  lastSummary = s;
  const tiles = [
    { label: '记录数 A / B Records', value: `${C.fmtNum(d.a.records)} / ${C.fmtNum(d.b.records)}` },
    { label: '键数 A / B Keys', value: `${C.fmtNum(d.a.keys)} / ${C.fmtNum(d.b.keys)}` },
    { label: '共同键 Common keys', value: C.fmtNum(s.common_keys) },
    { label: '完全一致 Same', value: C.fmtNum(s.same), cls: 'good' },
    { label: '值不同 Different', value: C.fmtNum(s.value_mismatch), cls: s.value_mismatch ? 'bad' : '' },
    { label: '仅 A / 仅 B Only A/B', value: `${C.fmtNum(s.only_in_a)} / ${C.fmtNum(s.only_in_b)}`, cls: (s.only_in_a + s.only_in_b) ? 'bad' : '' },
    { label: '重复键 A / B Dup keys', value: `${C.fmtNum(d.a.duplicate_keys)} / ${C.fmtNum(d.b.duplicate_keys)}` },
  ];
  const banner = s.identical
    ? '<div class="banner good">✓ 两个作业的结果完全一致 Results are identical</div>'
    : `<div class="banner bad">发现 ${C.fmtNum(s.diffs_total)} 个键存在差异 ${C.fmtNum(s.diffs_total)} differing key(s)</div>`;
  document.getElementById('stats').innerHTML =
    tiles.map(t => `<div class="stat"><div class="label">${t.label}</div><div class="value ${t.cls || ''}" style="font-size:20px">${t.value}</div></div>`).join('') +
    `<div style="grid-column:1/-1">${banner}</div>`;
}

function renderTypeFilter() {
  const s = lastSummary || {};
  const kinds = [
    ['', `全部 All (${C.fmtNum(s.diffs_total || 0)})`],
    ['value_mismatch', `值不同 Different (${C.fmtNum(s.value_mismatch || 0)})`],
    ['only_in_a', `仅 A Only A (${C.fmtNum(s.only_in_a || 0)})`],
    ['only_in_b', `仅 B Only B (${C.fmtNum(s.only_in_b || 0)})`],
  ];
  document.getElementById('type-filter').innerHTML = kinds.map(([k, label]) =>
    `<span class="pill${typeFilter === k ? ' active' : ''}" data-k="${k}">${label}</span>`).join('');
  document.querySelectorAll('#type-filter .pill').forEach(p => {
    p.addEventListener('click', () => { typeFilter = p.getAttribute('data-k'); resetAndRun(); });
  });
}

function renderDiffs(diffs) {
  document.getElementById('diffs').innerHTML = diffs.length ? C.table([
    { key: 'key', label: '键 Key', render: r => `<b>${C.esc(r.key)}</b>` + (r.count_a > 1 || r.count_b > 1 ? ` <span class="badge muted">×${Math.max(r.count_a, r.count_b)}</span>` : '') },
    { key: 'type', label: '类型 Type', render: r => `<span class="badge ${TYPE_BADGE[r.type] || 'muted'}">${TYPE_LABEL[r.type] || r.type}</span>` },
    { key: 'a', label: '作业 A 值 Value A', render: r => recCell(r.a !== undefined ? r.a : (r.records_a || [])[0]) },
    { key: 'b', label: '作业 B 值 Value B', render: r => recCell(r.b !== undefined ? r.b : (r.records_b || [])[0]) },
    { key: 'detail', label: '差异明细 Detail', render: detailCell },
  ], diffs, { rowClass: r => TYPE_ROW[r.type] || '' })
    : C.empty('当前筛选下没有差异 No differences under the current filter');
}

function renderPager() {
  const start = lastTotal ? offset + 1 : 0;
  const end = Math.min(offset + LIMIT, lastTotal);
  document.getElementById('pager').innerHTML = `
    <button class="btn small" id="pg-prev" ${offset <= 0 ? 'disabled' : ''}>‹ 上一页 Prev</button>
    <span class="muted small">${C.fmtNum(start)}–${C.fmtNum(end)} / ${C.fmtNum(lastTotal)} 条差异 diffs</span>
    <button class="btn small" id="pg-next" ${end >= lastTotal ? 'disabled' : ''}>下一页 Next ›</button>`;
  const prev = document.getElementById('pg-prev');
  const next = document.getElementById('pg-next');
  if (prev) prev.addEventListener('click', () => { offset = Math.max(0, offset - LIMIT); runCompare(); });
  if (next) next.addEventListener('click', () => { offset += LIMIT; runCompare(); });
}

// ---------------------------------------------------------------------------
// Fetch + render
// ---------------------------------------------------------------------------
async function runCompare() {
  if (!jobA || !jobB) return;
  const tol = parseFloat(document.getElementById('tolerance').value) || 0;
  const qs = `a=${encodeURIComponent(jobA)}&b=${encodeURIComponent(jobB)}` +
    `&type=${encodeURIComponent(typeFilter)}&q=${encodeURIComponent(query)}` +
    `&offset=${offset}&limit=${LIMIT}&tolerance=${tol}`;
  document.getElementById('dl').href = `/api/compare/download?a=${encodeURIComponent(jobA)}&b=${encodeURIComponent(jobB)}&tolerance=${tol}`;
  let d;
  try { d = await API.get('/api/compare?' + qs); } catch (e) {
    document.getElementById('diffs').innerHTML = C.empty('对比失败 Compare failed: ' + e.message);
    return;
  }
  lastTotal = d.diffs_total || 0;
  renderWarnings(d.warnings);
  renderStats(d);
  renderTypeFilter();
  renderDiffs(d.diffs || []);
  renderPager();
}

function resetAndRun() {
  offset = 0;
  runCompare();
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
document.getElementById('run').addEventListener('click', resetAndRun);
document.getElementById('swap').addEventListener('click', () => {
  const t = jobA; jobA = jobB; jobB = t;
  buildPickers();
  resetAndRun();
});
document.getElementById('tolerance').addEventListener('change', resetAndRun);
document.getElementById('key-search').addEventListener('input', e => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { query = e.target.value.trim(); resetAndRun(); }, 300);
});

loadJobs();
