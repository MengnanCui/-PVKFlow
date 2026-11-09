// QFLS 成像（P-54 … P-58）：一次处理 = 一个独立输出目录；这一页只是把它读出来看。
//
//   顶上一条：看哪次处理 · 处理新数据… · 生成演示数据 · 下载（CSV / README）· 删除
//   四个标签：总览 / 排名与筛选 / 多点对比 / 方法说明     ← 地址栏 #qfls/rank 这样记
//   筛选条（方法说明页没有）：实验、物镜、等级、异常类型、只看异常、搜编号；「导出当前筛选」
//   单点详情是下钻页 #qfls-point/<run>/<key>
//
// **不同物镜的绝对 QFLS 和面积占比不可直接比较**（P-58）—— 总览按物镜分开画，对比时物镜不同会提示。

import { api } from '../api.js';
import { h, mount, render, empty, errorBox, toast, modal, confirmDialog, busy, fmtTime, fmtBytes, skeletonRows, latest } from '../ui.js';
import { rowsToCsv } from '../csv.js';
import { S, openRun, clearRun, rememberedRun, filtered, filterActive, FLAG_TEXT, download } from './qfls-state.js';
import { overviewTab, rankTab, compareTab } from './qfls-views.js';
import { methodTab } from './qfls-method.js';

export const meta = {
  id: 'qfls',
  title: 'QFLS 成像',
  desc: '准费米能级分裂成像：逐点校验、指标、评分、排名、对比',
};

const TABS = [['overview', '总览'], ['rank', '排名与筛选'], ['compare', '多点对比'], ['method', '方法说明']];
const refs = {};
let polling = null;
const runTicket = latest();

export function view(host, ctx) {
  refs.host = host;
  refs.nav = ctx.nav;
  refs.alive = ctx.alive || (() => true);
  refs.tab = TABS.some(([t]) => t === ctx.arg) ? ctx.arg : (refs.tab || 'overview');
  ctx.signal?.addEventListener('abort', () => { clearTimeout(polling); polling = null; });
  return render(host, () => api.qflsRuns(), ({ runs }) => {
    refs.runs = runs;
    const pending = runs.find((r) => r.status === 'running');
    const want = S.runId && runs.some((r) => r.run_id === S.runId && r.status === 'ok') ? S.runId
      : runs.find((r) => r.run_id === rememberedRun() && r.status === 'ok')?.run_id
      || runs.find((r) => r.status === 'ok')?.run_id;
    refs.body = h('div');
    const page = h('div', refs.bar = h('div'), refs.progress = h('div'), refs.body);
    drawBar();
    if (pending) resume(pending.run_id);
    if (want) selectRun(want);
    else if (refs.tab === 'method') drawTabs();
    else mount(refs.body, firstUse());
    return page;
  });
}

// ------------------------------------------------------------------ 顶栏
function drawBar() {
  const runs = refs.runs || [];
  const sel = h('select.select.select-sm.qfls-run-sel', {
    'aria-label': '看哪次处理',
    onchange: (e) => selectRun(e.target.value),
  },
    !runs.length ? h('option', { value: '' }, '还没有处理过') : null,
    ...runs.map((r) => h('option', { value: r.run_id, selected: r.run_id === S.runId, disabled: r.status !== 'ok' },
      `${r.title} · ${fmtTime(r.created_at)} · ${r.status === 'ok' ? `${r.n_frames} 帧` : runStatus(r.status)}`)));
  mount(refs.bar, h('div.qfls-bar',
    h('label.inline-field', h('span.small.muted', '处理结果'), sel),
    h('span.grow'),
    h('button.btn.btn-sm', { onclick: openProcessDialog }, '处理新数据…'),
    h('button.btn.btn-sm', { onclick: (e) => startDemo(e.currentTarget), title: '按你给的目录 / 文件格式造 60 帧假想数据，然后处理' },
      '生成演示数据'),
    S.runId ? h('a.btn.btn-sm.btn-ghost', { href: api.qflsFile(S.runId, 'points.csv'), download: '' }, '全部样点 CSV') : null,
    S.runId ? h('a.btn.btn-sm.btn-ghost', { href: api.qflsFile(S.runId, 'README.md'), download: '' }, 'README') : null,
    S.runId ? h('button.btn.btn-sm.btn-ghost', { onclick: removeRun, title: '只删这次处理的输出目录，不碰原始数据' }, '删除') : null));
}

const runStatus = (s) => ({ running: '处理中', failed: '失败', cancelled: '已停止' }[s] || s);

async function selectRun(runId) {
  const fresh = runTicket();
  mount(refs.body, skeletonRows(6));
  try {
    await openRun(runId);
    if (!fresh() || !refs.alive()) return;
    drawBar();
    drawTabs();
  } catch (err) {
    if (fresh()) mount(refs.body, errorBox(err, () => selectRun(runId)));
  }
}

function firstUse() {
  return h('div.panel.panel-body',
    h('div.strong', '还没有处理过 QFLS 数据'),
    h('p.small.muted.mt-2.measure',
      '选一个数据根目录（实验 / 批次哈希 / slotN / pointN / QFLS / 0-物镜X-405R-光强-时间戳-uuid.txt），平台按层级识别、逐帧校验、',
      '算指标和综合分，结果写进工作区里一个独立的输出目录 —— 原始数据只读，一个字节都不改。'),
    h('p.small.muted.mt-2.measure', '手头没有数据的话，先生成一份演示数据看看：全部是程序按你描述的格式造的假想数据。'),
    h('div.row.gap-2.mt-3',
      h('button.btn.btn-primary.btn-sm', { onclick: (e) => startDemo(e.currentTarget) }, '生成演示数据并处理'),
      h('button.btn.btn-sm', { onclick: openProcessDialog }, '选数据根目录…')));
}

// ------------------------------------------------------------------ 标签 + 筛选
function drawTabs() {
  const tabs = h('div.qfls-tabs', { role: 'tablist' }, ...TABS.map(([id, label]) =>
    h('button.qfls-tab', {
      role: 'tab', 'aria-selected': String(id === refs.tab),
      onclick: () => { refs.tab = id; refs.nav('qfls', { arg: id }); },
    }, label, id === 'compare' && S.compare.length ? h('span.chip-count', ` ${S.compare.length}`) : null)));
  refs.filterHost = h('div');
  refs.tabHost = h('div.mt-4');
  mount(refs.body, tabs, refs.filterHost, refs.tabHost);
  drawFilter();
  drawTab();
}

export function refresh() {
  if (refs.tabHost?.isConnected) { drawFilter(); drawTab(); }
}

function drawTab() {
  const ctx = { nav: refs.nav, refresh, openPoint, redrawTabs: drawTabs };
  if (!S.summary && refs.tab !== 'method') { mount(refs.tabHost, firstUse()); return; }
  const fn = { overview: overviewTab, rank: rankTab, compare: compareTab, method: methodTab }[refs.tab];
  mount(refs.tabHost, fn(ctx));
}

function openPoint(key) {
  refs.nav('qfls-point', { arg: `${S.runId}/${key}` });
}

function chip(label, on, onclick, count = null, title = null) {
  return h('button.chip-toggle', { 'aria-pressed': String(on), onclick, title },
    h('span.chip-label', label), count !== null ? h('span.chip-count', String(count)) : null);
}

function drawFilter() {
  if (refs.tab === 'method' || !S.points) { mount(refs.filterHost); return; }
  const f = S.filter;
  const all = S.points;
  const rows = filtered();
  const toggle = (set, v) => () => { set.has(v) ? set.delete(v) : set.add(v); refresh(); };
  const flags = Object.entries(S.summary.flags || {}).sort((a, b) => b[1] - a[1]);
  const search = h('input.input.input-sm', {
    placeholder: '搜编号 / 文件名', value: f.q, 'aria-label': '搜编号或文件名',
    oninput: (e) => { f.q = e.target.value; clearTimeout(refs.qTimer); refs.qTimer = setTimeout(() => { refresh(); refocus(); }, 200); },
  });
  const refocus = () => { const el = refs.filterHost.querySelector('input'); if (el) { el.focus(); el.setSelectionRange(f.q.length, f.q.length); } };
  mount(refs.filterHost, h('div.qfls-filter.panel.panel-body',
    h('div.qfls-frow', h('span.qfls-flabel', '实验'),
      ...S.summary.experiments.map((e) => chip(`${e.code} ${e.name}`, f.exps.has(e.name), toggle(f.exps, e.name),
        all.filter((p) => p.experiment === e.name).length))),
    h('div.qfls-frow', h('span.qfls-flabel', '物镜'),
      ...S.summary.objectives.map((o) => chip(o, f.objs.has(o), toggle(f.objs, o), all.filter((p) => p.objective === o).length)),
      h('span.qfls-flabel.ml', '等级'),
      ...['A', 'B', 'C', 'D', '无效'].map((g) => chip(g, f.grades.has(g), toggle(f.grades, g),
        all.filter((p) => (p.grade || '无效') === g).length))),
    h('div.qfls-frow', h('span.qfls-flabel', '异常'),
      chip('只看异常', f.anomalyOnly, () => { f.anomalyOnly = !f.anomalyOnly; refresh(); }, all.filter((p) => p.anomaly).length,
           '有 warn / error 级标记的样点'),
      ...flags.map(([code, n]) => chip(FLAG_TEXT[code] || code, f.flag === code,
        () => { f.flag = f.flag === code ? null : code; refresh(); }, n))),
    h('div.qfls-frow',
      search,
      h('span.small.muted', `筛出 ${rows.length} / ${all.length} 个样点`),
      filterActive() ? h('button.btn.btn-xs.btn-ghost', {
        onclick: () => { f.exps.clear(); f.objs.clear(); f.grades.clear(); f.flag = null; f.anomalyOnly = false; f.q = ''; refresh(); },
      }, '清空筛选') : null,
      h('span.grow'),
      h('button.btn.btn-sm', { onclick: exportFiltered, disabled: !rows.length, title: '当前筛选出来的样点，含全部指标和组内 z' },
        `导出当前筛选（${rows.length}）`))));
}

const EXPORT_COLS = [
  ['rank', '排名'], ['code', '编号'], ['experiment', '实验'], ['batch', '批次'], ['slot', 'slot'], ['point', 'point'],
  ['objective', '物镜'], ['laser', '激光'], ['intensity', '光强'], ['timestamp', '时间'], ['valid', '有效'],
  ['score', '综合分'], ['grade', '等级'], ['anomaly', '异常'], ['flag_codes', '标记'], ['error', '无效原因'],
  ['mean', '均值 eV'], ['median', '中位数 eV'], ['std', '标准差 eV'], ['p90_p10', 'P90-P10 eV'], ['p99_p1', 'P99-P1 eV'],
  ['edge_minus_center', '边缘-中心 eV'], ['low_abs_frac', '低荧光绝对占比'], ['low_2s_frac', '低于中位数-2σ占比'],
  ['high_2s_frac', '高于中位数+2σ占比'], ['low_cc_count', '低值连通域数'], ['low_cc_max_px', '最大低值连通域 px'],
  ['low_cc_max_frac', '最大低值连通域占比'], ['scratch_count', '划痕候选数'], ['bright_density', '亮点密度 /Mpx'],
  ['morans_i', "Moran's I"], ['variogram_range_px', '变程 px'], ['variogram_range_um', '变程 µm'],
  ['rows', '行'], ['cols', '列'], ['rel', '文件'],
];

function exportFiltered() {
  const rows = filtered();
  const zKeys = ['median', 'p90_p10', 'low_2s_frac', 'morans_i', 'score'];
  const header = [...EXPORT_COLS.map(([, l]) => l), ...zKeys.map((k) => `组内z_${k}`)];
  const body = rows.map((p) => [
    ...EXPORT_COLS.map(([k]) => (Array.isArray(p[k]) ? p[k].join(';') : p[k] ?? '')),
    ...zKeys.map((k) => p.z?.[k] ?? ''),
  ]);
  const tag = filterActive() ? '筛选' : '全部';
  download(`QFLS_${S.runId}_${tag}_${rows.length}点.csv`, rowsToCsv(header, body));
  toast(`导出了 ${rows.length} 个样点`, 'ok');
}

// ------------------------------------------------------------------ 处理 / 演示数据 / 删除
async function startDemo(btn) {
  if (!await confirmDialog('生成一份假想的 QFLS 演示数据（60 帧、304 × 456，约 65 MB，放在工作区 qfls_demo/ 里，每次重新生成）并处理。大约一分钟。继续吗？',
    { confirmLabel: '生成并处理' })) return;
  busy(btn, true);
  try {
    const r = await api.qflsStart({ demo: true });
    watch(r.task.task_id, r.run_id);
  } catch (err) {
    toast(err.message, 'danger');
  } finally { busy(btn, false); }
}

function watch(taskId, runId) {
  refs.taskId = taskId;
  clearTimeout(polling);
  const tick = async () => {
    if (!refs.alive()) return;
    let t;
    try { t = await api.getTask(taskId); } catch (err) { mount(refs.progress, errorBox(err)); return; }
    if (!refs.alive()) return;
    mount(refs.progress, h('div.progress-card.mb-4',
      h('div.row-between', h('div.strong', t.title), h('button.btn.btn-xs', {
        disabled: t.done, onclick: () => api.cancelTask(taskId).then(tick),
      }, '停止')),
      h('div.progress-bar', h('i', { style: { width: `${t.percent ?? 0}%` } })),
      h('div.xsmall.muted.truncate', t.message || '排队中…')));
    if (!t.done) { polling = setTimeout(tick, 700); return; }
    refs.taskId = null;
    mount(refs.progress);
    if (t.status === 'ok') {
      toast(`处理完了：${t.result.n_frames} 帧，${t.result.n_valid} 帧有效，${t.result.n_anomaly} 个有异常标记`, 'ok', 6000);
      refs.runs = (await api.qflsRuns()).runs;
      selectRun(runId);
    } else {
      mount(refs.progress, h('div.notice.notice-danger.mb-4', h('div.grow',
        h('div', t.status === 'cancelled' ? '处理停了（已经做完的那部分留在输出目录里）' : '处理失败'),
        t.error ? h('pre', t.error.split('\n\n')[0]) : null)));
      refs.runs = (await api.qflsRuns()).runs;
      drawBar();
    }
  };
  tick();
}

/** 刷新页面后还在跑的那次处理：从任务列表里把它找回来，接着显示进度。 */
async function resume(runId) {
  if (refs.taskId) { watch(refs.taskId, runId); return; }
  try {
    const { tasks } = await api.listTasks();
    const t = tasks.find((x) => x.params?.run_id === runId && !x.done);
    if (t && refs.alive()) watch(t.task_id, runId);
  } catch { /* 找不到就算了：下拉里它显示「处理中」 */ }
}

async function removeRun() {
  if (!await confirmDialog(`删除这次处理的输出目录（${S.runId}）？原始数据不受影响。`, { confirmLabel: '删除', danger: true })) return;
  try {
    await api.qflsDelete(S.runId);
    clearRun();
    toast('删了', 'ok');
    refs.nav('qfls', { arg: refs.tab });
  } catch (err) { toast(err.message, 'danger'); }
}

/** 选数据根目录：浏览本机目录 → 扫描预览（认出了什么、跳过了什么）→ 开始处理。 */
function openProcessDialog() {
  const body = h('div');
  let cur = null;
  const ticket = latest();
  const startBtn = h('button.btn.btn-primary', { disabled: true, onclick: () => start() }, '开始处理');
  const m = modal({
    title: '处理 QFLS 数据', width: '880px', body,
    foot: [h('span.small.muted.grow', '只读原始数据；结果写进工作区 qfls/ 下一个新目录'),
           h('button.btn', { onclick: () => m.close() }, '取消'), startBtn],
  });

  async function roots() {
    const fresh = ticket();
    mount(body, skeletonRows(3));
    try {
      const { roots: rs } = await api.roots();
      if (!fresh()) return;
      const paste = h('input.input.mono', { placeholder: 'D:\\QFLS\\数据根目录', 'aria-label': '数据根目录路径' });
      paste.addEventListener('keydown', (e) => { if (e.key === 'Enter' && paste.value.trim()) dir(paste.value.trim()); });
      mount(body,
        h('p.small.muted', '选数据根目录（下面是一个个实验文件夹）。进到那个目录后点「扫描这个目录」，先看看认出了什么。'),
        h('div.field.mt-3', h('label.field-label', '直接粘贴路径'),
          h('div.row.gap-2', paste, h('button.btn', { onclick: () => paste.value.trim() && dir(paste.value.trim()) }, '打开'))),
        h('div.panel.mt-3', h('div.list', ...rs.map((r) => h('button.list-row', { onclick: () => dir(r.path) },
          h('div.grow', h('div.name', r.name), h('div.meta.mono', r.path)))))));
    } catch (err) { mount(body, errorBox(err, roots)); }
  }

  async function dir(path) {
    const fresh = ticket();
    startBtn.disabled = true;
    mount(body, skeletonRows(5));
    try {
      const d = await api.browse(path);
      if (!fresh()) return;
      cur = d.path;
      mount(body,
        h('div.row.gap-2',
          h('button.btn.btn-sm', { onclick: roots }, '起始位置'),
          d.parent ? h('button.btn.btn-sm', { onclick: () => dir(d.parent) }, '上一层') : null,
          h('div.grow.mono.small.truncate', { title: d.path }, d.path),
          h('button.btn.btn-sm.btn-primary', { onclick: () => preview(d.path) }, '扫描这个目录')),
        h('div.panel.mt-3', { style: { maxHeight: '46vh', overflow: 'auto' } },
          d.dirs.length ? h('div.list', ...d.dirs.map((x) => h('button.list-row', { onclick: () => dir(x.path) },
            h('span.muted', '▸'), h('div.grow.name', x.name))))
            : empty('这个目录下没有子文件夹')));
    } catch (err) { mount(body, errorBox(err, roots, '回到起始位置')); }
  }

  async function preview(path) {
    const fresh = ticket();
    mount(body, skeletonRows(5));
    try {
      const sc = await api.qflsScan(path);
      if (!fresh()) return;
      cur = path;
      startBtn.disabled = !sc.n_frames;
      mount(body,
        h('div.row.gap-2', h('button.btn.btn-sm', { onclick: () => dir(path) }, '← 回到目录'),
          h('div.grow.mono.small.truncate', path)),
        sc.n_frames
          ? h('div.notice.notice-accent.mt-3', h('div.grow',
              `认出 ${sc.experiments.length} 个实验、${sc.n_frames} 帧（${sc.objectives.join('、')}，共 ${fmtBytes(sc.total_bytes)}）。`,
              h('span.muted', ` 跳过 ${sc.skipped.length} 处、${sc.artifacts} 个历史产物。`)))
          : h('div.notice.notice-warn.mt-3', h('div.grow', '这里没认出 QFLS 数据。下面是每一处没认出的原因。')),
        sc.experiments.length ? h('table.data.mt-3', h('thead', h('tr', h('th', '实验'), h('th', '批次'), h('th.num', '点位'), h('th.num', '帧'))),
          h('tbody', ...sc.experiments.map((e) => h('tr', h('td', e.name, e.name !== e.raw_name ? h('span.xsmall.dim', `（磁盘上：${e.raw_name}）`) : null),
            h('td.small', e.batches.map((b) => b.name).join('、')), h('td.num', String(e.n_points)), h('td.num', String(e.n_frames)))))) : null,
        sc.renamed.length ? h('div.small.mt-3', h('span.strong', '修过的乱码名：'),
          ...sc.renamed.map((r) => h('div.mono.xsmall', `${r.raw} → ${r.fixed}（${r.how}）`))) : null,
        sc.skipped.length ? h('details.mt-3', h('summary.small', `跳过的 ${sc.skipped.length} 处`),
          h('table.data', h('tbody', ...sc.skipped.slice(0, 200).map((s) => h('tr', h('td.mono.xsmall', s.path), h('td.xsmall.muted', s.reason)))))) : null);
    } catch (err) { mount(body, errorBox(err, () => dir(path), '回到目录')); }
  }

  async function start() {
    busy(startBtn, true);
    try {
      const r = await api.qflsStart({ root: cur });
      m.close();
      watch(r.task.task_id, r.run_id);
    } catch (err) {
      toast(err.message, 'danger');
      busy(startBtn, false);
    }
  }
  roots();
}

