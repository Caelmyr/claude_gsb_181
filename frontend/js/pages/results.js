/* 结果导出 Results */
Components.init('results');
const C = Components;

let currentJob = '';
const compareState = {
  jobs: [],
  left: '',
  right: '',
  page: 1,
  pageSize: 50,
  summary: null,
};

async function render() {
  if (!currentJob) return;
  let d;
  try { d = await API.get('/api/jobs/' + currentJob + '/results?limit=100'); } catch (e) { return; }

  document.getElementById('dl-json').href = '/api/jobs/' + currentJob + '/results/download?format=json';
  document.getElementById('dl-csv').href = '/api/jobs/' + currentJob + '/results/download?format=csv';

  document.getElementById('stats').innerHTML = [
    { label: '结果记录 Total records', value: C.fmtNum(d.total) },
    { label: '分区数 Partitions', value: d.partitions.length },
    { label: '作业状态 Status', value: d.status },
    { label: '是否截断 Truncated', value: d.truncated ? '是 yes' : '否 no' },
  ].map(s => `<div class="stat"><div class="label">${s.label}</div><div class="value">${C.esc(s.value)}</div></div>`).join('');

  document.getElementById('partitions').innerHTML = d.partitions.length
    ? C.table([
        { key: 'partition_name', label: '分区 Partition', render: r => `<span class="mono">${C.esc(r.partition_name)}</span>` },
        { key: 'count', label: '记录数 Count', render: r => C.fmtNum(r.count), num: true },
        { key: 'task_id', label: 'Reduce 任务 Task', render: r => `<span class="mono">${C.esc(r.task_id)}</span>` },
      ], d.partitions)
    : C.empty('暂无结果 No results — 作业可能尚未完成');

  const records = d.records || [];
  document.getElementById('preview').innerHTML = records.length
    ? C.table([
        { key: 'key', label: 'Key', render: r => `<b>${C.esc(r.key)}</b>` },
        { key: 'value', label: 'Value', render: r => C.valueCell(r) },
      ], records)
    : C.empty('暂无结果 No results');
}

C.jobPicker('job-picker', (id) => { currentJob = id; render(); });
C.poll(render, 3000).start();

// ---------------------------------------------------------------------------
// Side-by-side result comparison
// ---------------------------------------------------------------------------
function compareJobOption(job) {
  return `<option value="${C.esc(job.job_id)}">${C.esc(job.name)} (${C.esc(job.job_id)})</option>`;
}

function selectedStatuses() {
  return Array.from(document.querySelectorAll('.compare-filters input[type="checkbox"]:checked'))
    .map(x => x.value);
}

function buildComparePicker(id, selected) {
  const sel = document.getElementById(id);
  sel.innerHTML = '<select class="job-select"><option value="">选择已完成作业…</option>' +
    compareState.jobs.map(compareJobOption).join('') + '</select>';
  const select = sel.querySelector('select');
  select.value = selected || '';
  return select;
}

async function initCompare() {
  try {
    const d = await API.get('/api/jobs');
    compareState.jobs = (d.jobs || []).filter(j => j.status === 'SUCCEEDED');
  } catch (e) {
    document.getElementById('compare-results').innerHTML = C.empty('无法加载作业列表 Cannot load jobs');
    return;
  }

  compareState.left = compareState.jobs[0]?.job_id || '';
  compareState.right = compareState.jobs[1]?.job_id || '';
  const left = buildComparePicker('compare-left-picker', compareState.left);
  const right = buildComparePicker('compare-right-picker', compareState.right);
  left.addEventListener('change', () => { compareState.left = left.value; compareState.page = 1; runCompare(); });
  right.addEventListener('change', () => { compareState.right = right.value; compareState.page = 1; runCompare(); });

  document.getElementById('compare-run').addEventListener('click', () => {
    compareState.page = 1;
    runCompare();
  });
  document.getElementById('compare-q').addEventListener('input', debounce(() => {
    compareState.page = 1;
    runCompare();
  }, 300));
  document.querySelectorAll('.compare-filters input[type="checkbox"]').forEach(x =>
    x.addEventListener('change', () => { compareState.page = 1; runCompare(); }));
  document.getElementById('compare-tolerance').addEventListener('change', () => {
    compareState.page = 1;
    runCompare();
  });
  document.getElementById('compare-key-field').addEventListener('change', () => {
    compareState.page = 1;
    runCompare();
  });

  runCompare();
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function compareStatus(status) {
  const labels = {
    changed: ['数值不同 Changed', 'bad'],
    left_only: ['仅 A 有 Left only', 'warn'],
    right_only: ['仅 B 有 Right only', 'warn'],
    duplicate: ['标识重复 Duplicate', 'aqua'],
    equal: ['相同 Equal', 'good'],
  };
  const [label, cls] = labels[status] || [status, 'muted'];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderCompareStats(s) {
  if (!s) return '';
  const tiles = [
    ['A 记录数', C.fmtNum(s.left_records)],
    ['B 记录数', C.fmtNum(s.right_records)],
    ['标识总数', C.fmtNum(s.total_keys)],
    ['相同', C.fmtNum(s.equal_keys)],
    ['数值不同', C.fmtNum(s.changed_keys)],
    ['仅 A 有', C.fmtNum(s.left_only_keys)],
    ['仅 B 有', C.fmtNum(s.right_only_keys)],
    ['重复标识', C.fmtNum(s.duplicate_identifier_keys)],
  ];
  return tiles.map(([label, value], i) =>
    `<div class="stat"><div class="label">${label}</div><div class="value ${i >= 4 && value !== '0' ? 'bad' : ''}">${value}</div></div>`
  ).join('');
}

function renderValue(value) {
  if (value === undefined) return '<span class="muted">缺失</span>';
  if (value === null) return '<span class="muted">null</span>';
  if (typeof value === 'object') return `<pre class="json-value">${C.esc(JSON.stringify(value, null, 2))}</pre>`;
  return C.esc(value);
}

function renderSide(rows) {
  if (!rows.length) return '<span class="muted">缺失 missing</span>';
  return rows.map((row, i) => {
    const loc = row.partition_name || row.task_id
      ? `<div class="diff-loc mono">#${row.index + 1} · ${C.esc(row.partition_name || '-')} · ${C.esc(row.task_id || '-')}${row.surplus ? ' · <b>额外</b>' : ''}</div>`
      : `<div class="diff-loc mono">#${row.index + 1}${row.surplus ? ' · <b>额外</b>' : ''}</div>`;
    return `<div class="side-record${row.surplus ? ' surplus' : ''}">${loc}<div class="side-value">${renderValue(row.payload)}</div></div>`;
  }).join('');
}

function renderDiffs(diffs) {
  if (!diffs.length) return '<span class="muted">-</span>';
  return diffs.map(d =>
    `<div><span class="mono diff-path">${C.esc(d.path || 'record')}</span>: ` +
    `<span class="diff-old">${renderValue(d.left)}</span> → <span class="diff-new">${renderValue(d.right)}</span></div>`
  ).join('');
}

function renderCompareGroups(groups) {
  if (!groups.length) return C.empty('没有符合筛选条件的差异 No matching differences');
  const rows = groups.map(g => {
    const duplicateNote = g.duplicate ? `<div class="small muted">重复数：A ${g.left_count} / B ${g.right_count}</div>` : '';
    return `<tr class="diff-row diff-${C.esc(g.status)}">
      <td>${compareStatus(g.status)}${duplicateNote}</td>
      <td><b class="mono">${C.esc(g.key)}</b></td>
      <td>${renderSide(g.left_rows)}</td>
      <td>${renderSide(g.right_rows)}</td>
      <td>${renderDiffs(g.differences)}</td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap diff-table-wrap"><table class="table diff-table">
    <thead><tr><th>状态</th><th>记录标识 Key</th><th>作业 A</th><th>作业 B</th><th>字段差异</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderPager(pagination) {
  if (!pagination || pagination.total === 0) return '';
  const from = (pagination.page - 1) * pagination.page_size + 1;
  const to = Math.min(pagination.total, pagination.page * pagination.page_size);
  return `<button class="btn small" id="compare-prev" ${pagination.page <= 1 ? 'disabled' : ''}>‹ 上一页</button>
    <span class="pager-info">${from}-${to} / ${pagination.total}，第 ${pagination.page}/${pagination.total_pages} 页</span>
    <button class="btn small" id="compare-next" ${pagination.page >= pagination.total_pages ? 'disabled' : ''}>下一页 ›</button>`;
}

async function runCompare() {
  const host = document.getElementById('compare-results');
  const pager = document.getElementById('compare-pager');
  if (!compareState.left || !compareState.right) {
    host.innerHTML = C.empty('请选择两个已成功完成的作业 Select two succeeded jobs');
    pager.innerHTML = '';
    document.getElementById('compare-stats').innerHTML = '';
    return;
  }
  if (compareState.left === compareState.right) {
    host.innerHTML = C.empty('请选择两个不同的作业 Select two different jobs');
    pager.innerHTML = '';
    document.getElementById('compare-stats').innerHTML = '';
    return;
  }

  const tolerance = Number(document.getElementById('compare-tolerance').value || 0);
  const keyField = document.getElementById('compare-key-field').value.trim() || 'key';
  if (!isFinite(tolerance) || tolerance < 0) {
    host.innerHTML = C.empty('数值容差必须是非负数 Tolerance must be non-negative');
    return;
  }

  const params = new URLSearchParams({
    left_job_id: compareState.left,
    right_job_id: compareState.right,
    key_field: keyField,
    numeric_tolerance: String(tolerance),
    page: String(compareState.page),
    page_size: String(compareState.pageSize),
  });
  const q = document.getElementById('compare-q').value.trim();
  if (q) params.set('q', q);
  selectedStatuses().forEach(s => params.append('status', s));

  try {
    const d = await API.get('/api/results/compare?' + params.toString());
    compareState.summary = d.summary;
    document.getElementById('compare-stats').innerHTML = renderCompareStats(d.summary);
    host.innerHTML = renderCompareGroups(d.groups);
    pager.innerHTML = renderPager(d.pagination);
    document.getElementById('compare-prev')?.addEventListener('click', () => { compareState.page--; runCompare(); });
    document.getElementById('compare-next')?.addEventListener('click', () => { compareState.page++; runCompare(); });
  } catch (e) {
    document.getElementById('compare-stats').innerHTML = '';
    pager.innerHTML = '';
    host.innerHTML = C.empty(e.message || '对比失败 Compare failed');
  }
}

initCompare();
