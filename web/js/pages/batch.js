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
import { figure, numberControl } from '../components/figure.js';

export const meta = {
  id: 'batch',
  parent: 'history',
  title: '批处理结果',
  desc: '',
};

// 超过这么多条就降级成分位数带。可以手动切回去。
const SPAGHETTI_LIMIT = 60;

// 第一张膜厚图固定看「刚开始那一秒」。这是每次都要看的那一格，不给控件。
const WIN_HEAD = [0, 1];

// 第二张图的窗口宽度。一秒是一个自然的读数单位，也和第一张对得起来。
const WIN_SPAN = 1;

// 第二张图默认落在**倒数第三秒**。
//
// 上一版把它写死成 27.5–28.5 s，而这批数据只测到 21.082 s ——
// 一打开就是一根空柱加一行「3 个样品在这个窗口里都没有数据」。
// 默认值不该是猜的：先看这批数据实际测到哪儿，再往回数三秒。
const TAIL_BACK = 3;

// 柱状图超过这么多个样品就没法读了，换成排序表格。照旧：降级要说出来。
const BAR_LIMIT = 24;

// 按批次着色时，同一批次的几片样品**必然同色** —— 颜色这个维度已经被批次
// 用掉了。线型是第二个维度，把同批次内的样品分开。
// 没有它，三条曲线两个批次时图上就是「两条一模一样的线」，这正是你看到的。
const DASHES = [null, '7 3', '2 3', '9 3 2 3'];

const S = {
  runId: null, taskId: null, detail: null, error: null,
  pins: [],          // 钉在这次对比上的 AI 分析
  // 特殊处理的两条曲线 + 膜厚曲线
  curves: { integral: null, slope: null, ot: null },
  mode: 'auto', groupBy: 'batch',
  // 时刻切片：窗口是**查询参数**，改一下不用重跑（膜厚曲线整条都存着了）
  win2: null,            // 第二张图的窗口。要先知道数据测到哪儿才定得下来
  win2Resolved: false,   // 已经按实际时间范围定过一次默认值了
  tMax: null,            // 这批数据里最晚的一帧
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
  S.win2 = null; S.win2Resolved = false; S.tMax = null;
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
  const wins = S.win2 ? [WIN_HEAD, S.win2] : [WIN_HEAD];
  try {
    S.slices = await api.batchSlices(S.runId, wins);
    S.tMax = maxT(S.slices.rows);
    // 第二张图的默认时刻要等这一步 —— 只有拿到数据才知道它测到哪儿。
    // 所以首屏是两次请求：第一张图先出来，第二张图紧接着补上。
    if (!S.win2Resolved) {
      S.win2Resolved = true;
      const w = tailWindow(S.tMax);
      if (w) { S.win2 = w; return loadSlices(); }
    }
  } catch (err) {
    S.sliceErr = err;
    S.slices = null;
  }
  S.sliceBusy = false;
  drawSlices();
}

const maxT = (rows) => {
  const ts = (rows || []).map((r) => r.t_max).filter(Number.isFinite);
  return ts.length ? Math.max(...ts) : null;
};

const r3 = (v) => Math.round(v * 1000) / 1000;

/**
 * 倒数第三秒的那一秒窗口。t_max = 21.082 → 最后一整秒是 20–21 s，
 * 往前数第三个就是 18–19 s。
 *
 * 数据不到四秒时退到「最后一秒」—— 硬套倒数第三会掉到负数上去，
 * 那就又变成一根空柱了。
 */
function tailWindow(tmax) {
  if (!Number.isFinite(tmax) || tmax <= 0) return null;
  const lo = Math.floor(tmax) - TAIL_BACK;
  if (lo > 0 && lo + WIN_SPAN <= tmax) return [lo, lo + WIN_SPAN];
  const a = r3(Math.max(0, tmax - WIN_SPAN));
  return a < tmax ? [a, r3(tmax)] : null;
}

const fmtWin = ([a, b]) => (a === b ? `${a} s` : `${a}–${b} s`);

function drawSlices() {
  const host = refs.sliceHost;
  if (!host) return;

  const head = h('div.section-head',
    h('div.section-title', '不同时刻的平均膜厚'),
    h('span.xsmall.dim', '换时刻不用重跑'));

  if (S.sliceErr) {
    mount(host, head, errorBox(S.sliceErr, loadSlices));
    return;
  }
  if (!S.slices) {
    mount(host, head, skeletonRows(3));
    return;
  }
  mount(host, head, sliceBody(S.slices));
}

/**
 * 两张图，不是一张。
 *
 * 「1 秒内」和「快干完时」问的是两件事：一个是刚铺上去的膜有多厚，
 * 一个是它干到最后剩多少。挤在同一张图里，同一根样品的两根柱子并排，
 * 你要在两个量级之间来回跳着读 —— 而它们本来就该各自有一条基线。
 */
function sliceBody(d) {
  const n = d.rows.length;
  // 柱子下面只写样品名。所有样品同批次时，标签里那截重复的 `ZG0013/` 前缀
  // 每根柱子写一遍，占掉的横向空间正是「名字被截断」的原因。
  const names = shortLabels(d.rows.map((r) => r.label));

  return h('div',
    h('div.fig-grid-2',
      sliceFigure(d, 0, '刚铺上去（0–1 s）', null, names),
      d.windows.length > 1
        ? sliceFigure(d, 1, `快干完时（${fmtWin([d.windows[1].from, d.windows[1].to])}）`,
                      timeControl(), names)
        : figure('快干完时', {
            body: h('div.notice',
              h('div.grow', h('div.small',
                S.tMax === null ? '这批数据里没有膜厚曲线，定不出第二个时刻。'
                                : '正在按这批数据的实际时间范围定第二个时刻…'))) })),
    sliceTable(d, d.windows.map((w) => fmtWin([w.from, w.to]))));
}

/** 一张时刻图。标题 / 控件 / 图 三行结构和样品页共用同一份版式。 */
function sliceFigure(d, si, title, ctl, names) {
  const n = d.rows.length;
  const missing = d.rows.filter((r) => r.values[si]?.mean === null).length;
  const win = fmtWin([d.windows[si].from, d.windows[si].to]);

  const body = n <= BAR_LIMIT
    ? barChart({
        groups: d.rows.map((r, i) => ({
          label: names[i],
          values: [{
            value: r.values[si]?.mean ?? null, ratio: r.values[si]?.ok_ratio,
            note: r.values[si]?.note, n_frames: r.values[si]?.n_frames,
            n_ok: r.values[si]?.n_ok,
          }],
        })),
        seriesLabels: [win],
        yLabel: '平均光学厚度 (nm)', unit: 'nm',
        // 两张图各用一个颜色 —— 同色的话，扫一眼很容易把两张当成同一张的两半
        colorFrom: si,
      }, { height: 250 })
    : h('div.notice',
        h('div.grow',
          h('div.small', `${fmtInt(n)} 个样品，柱状图挤不下（超过 ${BAR_LIMIT} 个就没法读了）`),
          h('p.xsmall.dim.mt-2', '下面的表格按第一个时间窗排序，一样能比。')));

  const note = h('div',
    h('div.chart-caption',
      `${fmtInt(n)} 个样品　窗口内`, h('span.strong', '全部帧'),
      '都参与平均，深色那截是其中可信的比例', infoDot('ot_status')),
    missing
      ? h('div.chart-caption.dim',
          `${fmtInt(missing)} 个样品在这个窗口里没有数据`,
          missing === n ? '（整批都没测到这个时刻 —— 不是算不出来，是数据里就没有）' : '')
      : null);

  return figure(title, { ctl, body, note });
}

/**
 * 第二张图的时刻控件。
 *
 * 上界卡在 `t_max - 1 s`：**滑到头也出不了数据范围**。上一版把窗口写死在
 * 28 s、而数据只到 21 s，就是因为这个数没有任何东西管着它。
 * 拖动时只动滑块，松手才打后端 —— 否则拖一次发几十个请求，
 * 而且每个响应回来都会把正在拖的那根滑块换掉。
 */
function timeControl() {
  if (!Number.isFinite(S.tMax) || S.tMax <= WIN_SPAN) return null;
  const max = r3(S.tMax - WIN_SPAN);
  const readout = h('span.xsmall.dim', fmtWin(S.win2));
  let pending = S.win2[0];
  return [
    numberControl('时刻起点', S.win2[0], 0, max, 0.5,
      (v) => { pending = v; readout.textContent = fmtWin([v, r3(v + WIN_SPAN)]); },
      (v) => { S.win2 = [v, r3(v + WIN_SPAN)]; pending = v; loadSlices(); }),
    h('span.row.gap-2.items-center',
      readout,
      h('span.xsmall.dim', `窗宽 ${WIN_SPAN} s · 这批数据测到 ${fmtNum(S.tMax, 2)} s`)),
  ];
}

/**
 * 把柱子下面的名字压到「真正区分这几个样品的那截」。
 *
 * 原样是 `ZG0013/ZG0013_2026072918354709_Mode5_202607291932_SPS100` —— 58 个字符，
 * 其中 `ZG0013/` 在同一个标签里就重复了一遍，`_SPS100` 三个样品全都一样。
 * 这些字符不区分任何东西，只是把要读的那截挤出画面（上一版就是这么被截断的）。
 *
 * 两刀都只在**能证明是冗余**的地方下：
 *   1. `批次/样品名` 里样品名本身就以批次开头 → 去掉前面那个 `批次/`
 *   2. 所有标签共有的结尾段（按 `_` 切）→ 一起去掉，至少留一段
 * 去完要是撞了名（本来就同名），就退回原样 —— 宁可长，不能指错样品。
 */
function shortLabels(labels) {
  const raw = labels.map((l) => String(l));
  if (raw.length < 2) return raw;

  let out = raw.map((l) => {
    const i = l.indexOf('/');
    if (i < 0) return l;
    const [batch, name] = [l.slice(0, i), l.slice(i + 1)];
    return name.startsWith(batch) ? name : l;
  });

  // 共有的结尾段。`_SPS100` 每根柱子写一遍，它不告诉你任何事。
  const parts = out.map((l) => l.split('_'));
  let tail = 0;
  while (parts.every((p) => p.length > tail + 1)
         && new Set(parts.map((p) => p[p.length - 1 - tail])).size === 1) {
    tail++;
  }
  if (tail) out = parts.map((p) => p.slice(0, p.length - tail).join('_'));

  return new Set(out).size === raw.length ? out : raw;
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
        select('着色', S.groupBy, [['batch', '按批次'], ['none', '逐条']],
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
      series: styled(all),
    }, { height: 300 });
    caption = `${fmtInt(n)} 条曲线`
      + (S.groupBy === 'batch'
          ? `，颜色分批次（${fmtInt(batchCount(all))} 个），同批次内按线型分样品`
          : '，逐条一色');
  }

  if (data.truncated) {
    caption += `　服务端只返回了前 ${fmtInt(data.returned)} 条，`
      + `另有 ${fmtInt(data.truncated)} 条没取。`;
  }
  return h('div', chart, h('div.chart-caption', caption));
}

/**
 * 给每条曲线定颜色维度和线型维度。
 *
 * 按批次着色时，`group` 交给 chart.js 去分配颜色（它对齐 matplotlib 的 12 色），
 * 线型在这里按「同批次内第几条」分配。两个维度合起来才能让 ZG0014 的两片样品
 * 在图上分得开 —— 只给颜色的话它俩是同一条线。
 */
function styled(series) {
  const seen = new Map();
  return series.map((s) => {
    if (S.groupBy !== 'batch') return { ...s, style: 'line', group: undefined, dash: null };
    const g = s.group || '';
    const n = seen.get(g) || 0;
    seen.set(g, n + 1);
    return { ...s, style: 'line', group: s.group, dash: DASHES[n % DASHES.length] };
  });
}

const batchCount = (series) => new Set(series.map((s) => s.group || '')).size;

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
        S.groupBy === 'batch'
          ? h('div.xsmall.dim.mt-1',
              '导出脚本按批次分色，同批次内按 matplotlib 规范的线型循环区分样品 —— '
              + '和屏幕上这张一致。')
          : null,
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
