// 批处理结果页：叠图 + 运行状态表 + 导出。
//
// 上千条曲线叠一张图是噪声不是图 —— 超过阈值自动降级成分位数带，
// 并在图注里写明降级了。不偷偷少画。

import { api } from '../api.js';
import { infoDot, withInfo } from '../components/info.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy,
  fmtInt, fmtNum, fmtTime, modal, sampleLabel,
} from '../ui.js';
import { xyChart, quantileBand, seriesColor, barChart } from '../chart.js';

export const meta = {
  id: 'batch',
  parent: 'history',
  title: '批处理结果',
  desc: '',
};

// 超过这么多条就降级成分位数带。可以手动切回去。
const SPAGHETTI_LIMIT = 60;

// 时刻切片的默认窗口。你举的两个例子：「1s 内」= 0–1 s，「28s」= 27.5–28.5 s。
// 超出样品时间范围的窗口不会被悄悄丢掉 —— 它会显示「超出范围」，
// 而「这批数据根本没测到 28 秒」本身就是你需要知道的信息。
const DEFAULT_WINDOWS = [[0, 1], [27.5, 28.5]];

// 柱状图超过这么多个样品就没法读了，换成排序表格。照旧：降级要说出来。
const BAR_LIMIT = 24;

const S = {
  runId: null, taskId: null, detail: null, error: null,
  pins: [],          // 钉在这次对比上的 AI 分析
  // 特殊处理的两条曲线 + 膜厚曲线
  curves: { integral: null, slope: null, ot: null },
  mode: 'auto', groupBy: 'batch',
  // 时刻切片：窗口是**查询参数**，改一下不用重跑（膜厚曲线整条都存着了）
  windows: DEFAULT_WINDOWS.map((w) => [...w]),
  slices: null, sliceErr: null, sliceBusy: false,
};

let refs = {};

export function actions(nav) {
  return [
    h('button.btn.btn-sm', { onclick: () => nav('history') }, '← 对比历史'),
    h('button.btn.btn-sm', { onclick: () => nav('process') }, '挑样品'),
  ];
}

export async function view(host, ctx) {
  S.detail = S.error = null;
  S.curves = { integral: null, slope: null, ot: null };
  S.slices = S.sliceErr = null;
  S.windows = DEFAULT_WINDOWS.map((w) => [...w]);
  S.pins = [];
  refs = { host, nav: ctx.nav };

  const arg = ctx.arg;
  if (!arg) {
    mount(host, empty('没有指定批处理', h('button.btn.btn-sm',
      { onclick: () => ctx.nav('process') }, '回到样品列表')));
    return;
  }

  // 参数可以是 task_… 也可以是 run_…。
  // 「选中几个样品 → 直接进对比页」那条路上，提交的一刻还没有 run_id ——
  // 让用户对着「正在跳转…」干等是没必要的，进来先看进度就是了。
  if (String(arg).startsWith('task_')) {
    S.taskId = arg;
    const done = await waitForTask(host, arg, ctx);
    if (!done) return;
    S.runId = done;
  } else {
    S.taskId = null;
    S.runId = arg;
  }

  mount(host, skeletonRows(8));
  try {
    S.detail = await api.batchDetail(S.runId);
  } catch (err) {
    mount(host, errorBox(err, () => view(host, ctx)));
    return;
  }

  const params = S.detail.run.params || {};
  document.querySelector('#pageTitle').textContent = params.title || '对比结果';
  document.querySelector('#pageDesc').textContent =
    `${fmtInt(params.n_samples || S.detail.children.length)} 个样品 · `
    + fmtTime(S.detail.run.started_at);

  const chartHost = h('div#chartHost');
  const tableHost = h('div#tableHost');
  const pinHost = h('div#pinHost');
  const sliceHost = h('div#sliceHost');
  mount(host, header(), pinHost,
        h('div.section', sliceHost),
        h('div.section', chartHost), h('div.section', tableHost));
  refs.chartHost = chartHost;
  refs.tableHost = tableHost;
  refs.pinHost = pinHost;
  refs.sliceHost = sliceHost;

  drawChart();
  drawTable();
  loadCurves();
  loadSlices();
  loadPins();
}

/**
 * 等这次批处理跑完，边等边显示进度。
 *
 * @returns parent_run_id；失败或取消时返回 null（错误已经画在页面上了）
 */
async function waitForTask(host, taskId, ctx) {
  const bar = h('i');
  const msg = h('div.small.muted.mt-2', '正在排队…');
  mount(host, h('div.section',
    h('div.panel.panel-body',
      h('div.strong', '正在对比'),
      h('div.progress-bar.mt-3', bar),
      msg,
      h('div.mt-3', h('button.btn.btn-sm', {
        onclick: async () => { await api.cancelTask(taskId).catch(() => {});
                               ctx.nav('process'); },
      }, '取消')))));

  for (;;) {
    let t;
    try {
      t = await api.getTask(taskId);
    } catch (err) {
      mount(host, errorBox(err, () => view(host, ctx)));
      return null;
    }
    const pct = t.total ? Math.round((t.progress / t.total) * 100) : 0;
    bar.style.width = `${pct}%`;
    msg.textContent = t.message || `${t.progress} / ${t.total}`;

    if (t.status === 'ok') return t.result?.parent_run_id || null;
    if (t.status === 'failed' || t.status === 'cancelled') {
      mount(host, errorBox(
        { message: t.error || '这次对比没跑成', detail: t.message },
        () => ctx.nav('process')));
      return null;
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}

// ------------------------------------------------------------------ 钉住的分析
//
// 「比完就没了、找不到在哪儿」的最后一环。对比本身靠 analysis_run 留住了，
// AI 对那次对比说过的话靠这里 —— 钉住的是正文快照，删掉对话也带不走它。
async function loadPins() {
  try {
    S.pins = (await api.pins(S.runId)).pins || [];
  } catch {
    S.pins = [];        // 钉住是锦上添花，取不到不该影响看结果
  }
  drawPins();
}

function drawPins() {
  if (!refs.pinHost) return;
  if (!S.pins.length) { clear(refs.pinHost); return; }
  mount(refs.pinHost, h('div.section',
    h('div.panel',
      h('div.panel-head',
        h('div.panel-title', `AI 分析 · ${fmtInt(S.pins.length)} 条`),
        h('span.xsmall.dim', '从 AI 助手里钉过来的')),
      h('div.panel-body',
        ...S.pins.map((p) => h('div.mb-3',
          h('div.pin-head',
            h('span.xsmall.dim',
              `${p.conversation_title || '已删除的对话'} · ${fmtTime(p.created_at)}`),
            h('button.ai-act', {
              onclick: async () => {
                await api.deletePin(p.pin_id);
                S.pins = S.pins.filter((x) => x.pin_id !== p.pin_id);
                drawPins();
                toast('已取消钉住');
              },
            }, '取消钉住')),
          h('div.pin-note', p.note)))))));
}

function header() {
  const d = S.detail;
  return h('div.section',
    h('div.metrics.metrics-4',
      metric('样品', d.children.length, '本次处理'),
      metric('成功', d.n_ok, d.n_ok === d.children.length ? '全部通过' : ''),
      metric('失败', d.n_failed, d.n_failed ? '见下表' : '无'),
      metric('曲线表', d.tables.length ? d.tables[0].n_rows : 0, '长表行数')),
    recipePanel());
}

/**
 * 配方面板。**进来先用默认值比出结果，参数摆在这儿随时改。**
 *
 * 原来是进对比之前先弹一个对话框填六个数 —— 可你在填的时候还没看到任何东西，
 * 六个数该填什么根本无从判断。现在反过来：先看到结果，觉得窗口不对再改。
 *
 * 改完是**新的一次对比**，旧的那次留在对比历史里。参数一改结果就变，
 * 原地覆盖的话你就没法说「上次那个窗口下是什么样」了。
 */
function recipePanel() {
  const recipe = { ...(S.detail.run.params || {}).recipe };
  const busyRef = {};

  const num = (key) => h('label.inline-field',
    h('span.small.muted', RECIPE_LABELS[key] || key),
    h('input.input.input-sm', {
      type: 'number', step: 'any', value: recipe[key] ?? '',
      style: { width: '84px' },
      oninput: (e) => { recipe[key] = Number(e.target.value); },
    }));

  const rerun = h('button.btn.btn-sm.btn-primary', {
    onclick: async (e) => {
      busy(e.target, true);
      try {
        const r = await api.batchRun({
          filter: (S.detail.run.params || {}).filter || {},
          recipe,
          title: `${S.detail.n_ok} 个样品 · ${recipe.band_min}–${recipe.band_max} nm`,
        });
        // 跳到新的一次 —— 旧的那次原样留在对比历史里
        refs.nav('batch', { arg: r.task.task_id });
      } catch (err) {
        toast(err.message, 'danger');
        busy(e.target, false);
      }
    },
  }, '按新参数重跑');
  busyRef.btn = rerun;

  return h('div.figure-ctl.mt-4', { style: { display: 'block' } },
    h('div.row.gap-3.wrap.items-end',
      h('span.small.strong', '参数'),
      num('band_min'), num('band_max'),
      infoDot('band'),
      h('span.sep-v'),
      num('integral_min'), num('integral_max'),
      num('slope_center'), num('slope_half_width'),
      h('span.grow'),
      rerun),
    h('div.xsmall.dim.mt-2',
      '改膜厚窗口会重算光学厚度，所以要重跑一次。',
      '下面「不同时刻的平均膜厚」里加减时间窗**不用**重跑 —— 整条膜厚曲线已经存下来了。'));
}

const RECIPE_LABELS = {
  band_min: '膜厚窗口起', band_max: '膜厚窗口止',
  integral_min: '积分起', integral_max: '积分止',
  slope_center: '斜率波长', slope_half_width: '斜率半宽',
};

const metric = (label, value, note) => h('div.metric',
  h('div.metric-label', label),
  h('div.metric-value' + (value ? '' : '.is-zero'), fmtInt(value)),
  note ? h('div.metric-note', note) : null);

// ------------------------------------------------------------------ 时刻切片对比
//
// 「不同样品在 1 秒内的平均膜厚」「在 28 秒的平均膜厚」—— 这是横向比样品，
// 跟上面那些「一个样品随时间怎么变」的曲线是两种问题。
//
// 时间窗是**查询参数**，不是配方：整条膜厚曲线在批处理时就算好存下了，
// 这里只是按窗口切一刀求平均。所以加一个「再看看 15 秒」是即时的，不用重跑。
async function loadSlices() {
  if (!refs.sliceHost) return;
  S.sliceBusy = true;
  S.sliceErr = null;
  drawSlices();
  try {
    S.slices = await api.batchSlices(S.runId, S.windows);
  } catch (err) {
    S.sliceErr = err;
    S.slices = null;
  }
  S.sliceBusy = false;
  drawSlices();
}

const fmtWin = ([a, b]) => (a === b ? `${a} s` : `${a}–${b} s`);

function drawSlices() {
  const host = refs.sliceHost;
  if (!host) return;

  const head = h('div.section-head',
    h('div.section-title', '不同时刻的平均膜厚'),
    h('span.xsmall.dim', '加减时间窗不用重跑'));

  if (S.sliceErr) {
    mount(host, head, errorBox(S.sliceErr, loadSlices));
    return;
  }

  mount(host, head, windowEditor(),
    S.sliceBusy && !S.slices ? skeletonRows(3)
      : S.slices ? sliceBody(S.slices) : null);
}

/** 时间窗编辑器。每个窗两个数字框，能加能删。 */
function windowEditor() {
  const row = ([lo, hi], i) => h('div.row.gap-1.items-center',
    h('input.input.input-sm', {
      type: 'number', step: 'any', value: lo, style: { width: '68px' },
      onchange: (e) => { S.windows[i][0] = Number(e.target.value); loadSlices(); },
    }),
    h('span.small.dim', '–'),
    h('input.input.input-sm', {
      type: 'number', step: 'any', value: hi, style: { width: '68px' },
      onchange: (e) => { S.windows[i][1] = Number(e.target.value); loadSlices(); },
    }),
    h('span.small.dim', 's'),
    S.windows.length > 1
      ? h('button.btn.btn-ghost.btn-xs', {
          title: '删掉这个时间窗',
          onclick: () => { S.windows.splice(i, 1); loadSlices(); },
        }, '✕')
      : null);

  return h('div.figure-ctl', { style: { display: 'block' } },
    h('div.row.gap-4.wrap.items-center',
      h('span.small.muted', '时间窗'),
      ...S.windows.map(row),
      h('button.btn.btn-sm', {
        disabled: S.windows.length >= 12,
        onclick: () => {
          // 新窗口接着最后一个往后排，省得每次都要从头改两个数
          const last = S.windows[S.windows.length - 1] || [0, 1];
          const span = Math.max(1, last[1] - last[0]);
          S.windows.push([last[1], last[1] + span]);
          loadSlices();
        },
      }, '+ 加一个时刻'),
      S.sliceBusy ? h('span.xsmall.dim', '算…') : null));
}

function sliceBody(d) {
  const labels = d.windows.map((w) => fmtWin([w.from, w.to]));
  const n = d.rows.length;

  // 每个窗口里「有几个样品根本没数据」—— 这句必须显眼。
  // 你的数据只测到 21 秒，问 28 秒时整列是空的，而那正是要告诉你的事。
  const missing = d.windows.map((_, si) =>
    d.rows.filter((r) => r.values[si]?.mean === null).length);

  const chart = n <= BAR_LIMIT
    ? barChart({
        groups: d.rows.map((r) => ({
          label: r.label,
          values: r.values.map((v) => ({
            value: v.mean, ratio: v.ok_ratio, note: v.note,
            n_frames: v.n_frames, n_ok: v.n_ok })),
        })),
        seriesLabels: labels,
        yLabel: '平均光学厚度 (nm)', unit: 'nm',
      }, { height: 300 })
    : h('div.notice',
        h('div.grow',
          h('div.small', `${fmtInt(n)} 个样品，柱状图挤不下（超过 ${BAR_LIMIT} 个就没法读了）`),
          h('p.xsmall.dim.mt-2', '下面的表格按第一个时间窗排序，一样能比。')));

  return h('div',
    chart,
    h('div.chart-caption',
      `${fmtInt(n)} 个样品 × ${d.windows.length} 个时间窗　窗口内`,
      h('span.strong', '全部帧'),
      '都参与平均，深色那截是其中可信的比例',
      infoDot('ot_status')),
    ...missing.map((m, si) => (m
      ? h('div.chart-caption.dim',
          `${labels[si]}：${fmtInt(m)} 个样品在这个窗口里没有数据`,
          m === n ? '（整批都没测到这个时刻 —— 不是算不出来，是数据里就没有）' : '')
      : null)),
    sliceTable(d, labels));
}

function sliceTable(d, labels) {
  // 按第一个窗口的均值排序：横向比的时候，谁高谁低应该一眼看得出来
  const rows = [...d.rows].sort((a, b) =>
    (b.values[0]?.mean ?? -Infinity) - (a.values[0]?.mean ?? -Infinity));

  return h('div.panel.panel-body.flush.mt-4',
    h('table.table',
      h('thead', h('tr',
        h('th', '样品'),
        ...labels.map((l) => h('th', l)),
        h('th', '测到'))),
      h('tbody', ...rows.map((r) => h('tr',
        h('td.strong', r.label),
        ...r.values.map((v) => cell(v)),
        h('td.mono.xsmall.dim', `${r.t_min}–${r.t_max} s`))))));
}

function cell(v) {
  if (v.mean === null) {
    return h('td', h('span.dim', '—'),
      v.note ? h('div.xsmall.dim', v.note) : null);
  }
  const pct = Math.round((v.ok_ratio ?? 0) * 100);
  return h('td',
    h('span.mono', `${fmtNum(v.mean, 0)} nm`),
    // 可信比例低的时候必须显眼 —— 均值本身看不出它是被噪声帧拉出来的
    h('div.xsmall' + (pct < 50 ? '.warn-text' : '.dim'),
      `${v.n_ok}/${v.n_frames} 帧可信${pct < 50 ? '　这个数不可靠' : ''}`));
}

// ------------------------------------------------------------------ 特殊处理对比
//
// 左右并排两张：谱斜率 vs 时间 | 波段积分 vs 时间。
// 对比看的就是这两条 —— 一次全画出来，不用先选一个再切另一个。
async function loadCurves() {
  await Promise.all(['integral', 'slope', 'ot'].map(async (col) => {
    try {
      S.curves[col] = await api.batchCurves(S.runId, { column: col, max_series: 1200 });
    } catch (err) {
      // 膜厚可能整个缺席（老的对比跑在加膜厚之前）。那不该把另外两张图也拖垮 ——
      // 缺的那一张自己显示原因就行。
      if (col === 'ot') S.curves.ot = { error: err };
      else S.error = err;
    }
    drawChart();
  }));
}

/** 屏幕上现在画的是叠图还是分位数带。导出必须跟它一致。 */
function effectiveMode(col) {
  const n = S.curves[col]?.series?.length || 0;
  return (S.mode === 'band' || (S.mode === 'auto' && n > SPAGHETTI_LIMIT)) ? 'band' : 'overlay';
}

function drawChart() {
  const host = refs.chartHost;
  if (!host) return;
  if (S.error) { mount(host, errorBox(S.error, loadCurves)); return; }

  mount(host,
    h('div.section-head',
      h('div.section-title', '特殊处理对比'),
      h('div.row.gap-3.wrap',
        h('span.row.gap-1',
          select('显示', S.mode,
            [['auto', `自动（>${SPAGHETTI_LIMIT} 条降级）`], ['all', '全部画出'],
             ['band', '强制分位数带']],
            (v) => { S.mode = v; drawChart(); }),
          infoDot('quantile_band')),
        select('着色', S.groupBy, [['batch', '按样品号'], ['none', '逐条']],
          (v) => { S.groupBy = v; drawChart(); }))),
    h('div.fig-grid-2',
      curveFigure('ot', '光学厚度 vs 时间'),
      curveFigure('slope', '谱斜率 vs 时间')),
    h('div.fig-grid-2.mt-6',
      curveFigure('integral', '波段积分 vs 时间'),
      h('div.figure')));
}

// 和样品页一样的三行结构：标题 / 功能 / 图。没有功能的那一格第二行空着，
// 但仍然占着 subgrid 的那一行 —— 左右两张图才会从同一条线开始。
function curveFigure(col, title) {
  const data = S.curves[col];
  const body = data?.error
    ? h('div.notice.notice-warn',
        h('div.grow',
          h('div.small', data.error.message),
          h('p.xsmall.dim.mt-2', '用上面的参数重跑一次就有了。')))
    : data ? paintCurve(col, data) : skeletonRows(4);
  return h('div.figure',
    h('div.figure-head', h('div.figure-title', COL_INFO[col]
      ? withInfo(title, COL_INFO[col]) : title)),
    h('div.figure-ctl', data && !data.error
      ? h('button.btn.btn-sm', { onclick: () => exportDialog(col) }, '导出脚本')
      : null),
    h('div.figure-body', body));
}

const COL_INFO = { ot: 'ot', slope: 'slope', integral: 'integral' };

function paintCurve(col, data) {
  const all = data.series;
  const n = all.length;
  const degrade = effectiveMode(col) === 'band';

  let chart, caption;
  if (degrade) {
    const band = quantileBand(all);
    // 带里再画几条代表曲线，让人看到个体形状而不只是统计量
    const step = Math.max(1, Math.floor(n / 6));
    const reps = all.filter((_, i) => i % step === 0).slice(0, 6)
      .map((s) => ({ ...s, group: undefined }));
    chart = xyChart({
      x_label: '时间 (s)', y_label: Y_LABEL[col], band,
      series: reps.map((s) => ({ ...s, style: 'line' })),
    }, { height: 300 });
    caption = `${fmtInt(n)} 条曲线 —— 显示中位数与四分位区间，另叠了 ${reps.length} 条代表曲线。`
      + `超过 ${SPAGHETTI_LIMIT} 条自动降级，因为上千条叠一起是噪声不是图。`;
  } else {
    chart = xyChart({
      x_label: '时间 (s)', y_label: Y_LABEL[col],
      series: all.map((s) => ({ ...s, style: 'line',
        group: S.groupBy === 'batch' ? s.group : undefined })),
    }, { height: 300 });
    caption = `${fmtInt(n)} 条曲线`
      + (S.groupBy === 'batch' ? '，按样品号着色（最多 12 组）' : '');
  }

  if (data.truncated) {
    caption += `　服务端只返回了前 ${fmtInt(data.returned)} 条，`
      + `另有 ${fmtInt(data.truncated)} 条没取。`;
  }
  return h('div', chart, h('div.chart-caption', caption));
}

const Y_LABEL = { integral: '积分强度 (a.u.·nm)', slope: 'dI/dλ (a.u./nm)',
                  ot: '光学厚度 OT = n·d·cosθ (nm)' };

function select(label, value, options, onChange) {
  return h('label.inline-field',
    h('span.small.muted', label),
    h('select.select.select-sm', { onchange: (e) => onChange(e.target.value) },
      ...options.map(([v, t]) => h('option', { value: v, selected: v === value }, t))));
}

// ------------------------------------------------------------------ 状态表
function drawTable() {
  const host = refs.tableHost;
  if (!host) return;
  const d = S.detail;
  const failed = d.children.filter((c) => c.status === 'failed');
  const warned = d.children.filter((c) => c.status === 'ok' && c.warnings?.length);

  mount(host,
    h('div.section-head',
      h('div.section-title', '运行状态'),
      h('div.section-note',
        d.n_failed ? `${fmtInt(d.n_failed)} 个失败` : '全部成功',
        warned.length ? ` · ${fmtInt(warned.length)} 个有警告` : '')),
    failed.length ? failedTable(failed) : null,
    warned.length ? warningBlock(warned) : null,
    allTable(d.children));
}

/** 失败的样品单独列在最上面 —— 跑 1000 个样品时这张表比图重要。 */
function failedTable(failed) {
  return h('div.panel.panel-body.flush.mb-3',
    h('table.data',
      h('thead', h('tr', h('th', '样品'), h('th', '批次'), h('th', '失败原因'))),
      h('tbody', ...failed.map((c) => h('tr',
        h('td.strong', sampleLabel(c.sample_name, c.batch)),
        h('td.small.muted', c.batch || '—'),
        h('td.small', { style: { color: 'var(--danger)' } },
          String(c.error || '').split('\n')[0]))))));
}

function warningBlock(warned) {
  const rows = warned.slice(0, 50).map((c) => h('tr',
    h('td.strong', sampleLabel(c.sample_name, c.batch)),
    h('td.small.muted', (c.warnings || []).join('；'))));
  return h('details.mb-3',
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      `${fmtInt(warned.length)} 个样品有警告`),
    h('div.panel.panel-body.flush.mt-2',
      h('table.data', h('tbody', ...rows))));
}

function allTable(children) {
  const rows = children.map((c) => h('tr',
    h('td', sampleLabel(c.sample_name, c.batch)),
    h('td.small.muted', c.batch || '—'),
    h('td', h('span.status.' + (c.status === 'ok' ? 'status-ok' : 'status-danger'),
              c.status === 'ok' ? '成功' : '失败')),
    h('td.num', fmtInt(c.n_results))));
  return h('details',
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      `全部 ${fmtInt(children.length)} 个样品`),
    h('div.panel.panel-body.flush.mt-2',
      { style: { maxHeight: '420px', overflow: 'auto' } },
      h('table.data',
        h('thead', h('tr', h('th', '样品'), h('th', '批次'), h('th', '状态'),
                     h('th.num', '结果数'))),
        h('tbody', ...rows))));
}

// ------------------------------------------------------------------ 导出
/** 下载之前先把脚本读一眼 —— 论文里那张图的来源，值得先看看它画的是什么。 */
async function previewScript(params) {
  try {
    const r = await api.batchExportPreview(S.runId, params);
    modal({
      title: 'plot.py',
      width: '860px',
      body: h('div',
        h('p.xsmall.dim',
          `${fmtInt(r.n_series)} 条曲线 · data.csv ${fmtInt(r.n_rows)} 行 · `,
          h('span.mono', r.columns.join(', '))),
        h('pre.code-block.mt-3', r.script)),
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}

function exportDialog(col) {
  // 「自动」模式下屏幕已经降级成分位数带了，导出就不能还是 90 条叠图 ——
  // 拿到手的图跟屏幕上看到的不是同一张，是最让人困惑的一种不一致。
  const mode = effectiveMode(col);
  const params = { column: col, mode, group_by: S.groupBy };
  const url = api.batchExportUrl(S.runId, params);
  const m = modal({
    title: '导出绘图脚本',
    width: '640px',
    body: h('div',
      h('p.small',
        '导出一个 zip：', h('span.mono', 'plot.py'), ' + ', h('span.mono', 'data.csv'),
        ' + ', h('span.mono', 'README.md'), '。'),
      h('p.small.muted.mt-3',
        '脚本按你的 matplotlib 规范生成 —— rcParams、12 色序列、标记、线型都在里面，',
        '解压出来直接 ', h('span.mono', 'python plot.py'), ' 就能出 300 dpi 的图。'),
      h('div.notice.notice-accent.mt-4',
        h('div.grow',
          h('div.small.strong', '为什么是脚本而不是让模型直接画'),
          h('p.xsmall.dim.mt-2',
            '平台内置图型不够用时你拿脚本走人随便改；',
            '而且论文里那张图是你自己能读、能改、能引用的代码画的 —— ',
            '不是模型在某个沙箱里跑出来的黑箱。'))),
      h('p.xsmall.dim.mt-3',
        '当前导出：', h('span.mono', `${Y_LABEL[col]} · `
          + (mode === 'band' ? '分位数带' : '叠图')
          + ` · 按${S.groupBy === 'batch' ? '批次' : '逐条'}着色`),
        mode === 'band' && S.mode === 'auto'
          ? '（跟图上一样，因为曲线超过阈值已经降级）' : null)),
    foot: [
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('button.btn', {
        onclick: (e) => { busy(e.target, true); previewScript(params)
          .finally(() => busy(e.target, false)); },
      }, '先读一眼 plot.py'),
      // 没有后端就打不出 zip（静态演示版）。这时候不放一个点了没反应的按钮，
      // 直接说清楚 —— 脚本本身还是真的，读得到。
      url
        ? h('a.btn.btn-primary', { href: url, download: '' }, '下载 zip')
        : h('span.xsmall.dim', '这个版本没有后端，打不出 zip'),
    ],
  });
}
