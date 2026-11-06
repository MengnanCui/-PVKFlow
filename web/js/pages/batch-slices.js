// 批处理结果页：时刻切片 —— 「刚开始那一秒」和「倒数第三秒」两张膜厚图。
//
// 时间窗是**查询参数**，不是配方：整条膜厚曲线在批处理时就算好存下了，
// 这里只是按窗口切一刀求平均，所以改窗口不用重跑。

import { api } from '../api.js';
import { infoDot } from '../components/info.js';
import { h, mount, skeletonRows, errorBox, fmtInt, fmtNum, latest } from '../ui.js';
import { barChart } from '../chart.js';
import { figure, numberControl } from '../components/figure.js';
import { S, refs, yLabel } from './batch-state.js';

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

// ------------------------------------------------------------------ 时刻切片对比
//
// 「不同样品在 1 秒内的平均膜厚」「在 28 秒的平均膜厚」—— 这是横向比样品，
// 跟上面那些「一个样品随时间怎么变」的曲线是两种问题。
//
// 时间窗是**查询参数**，不是配方：整条膜厚曲线在批处理时就算好存下了，
// 这里只是按窗口切一刀求平均。所以加一个「再看看 15 秒」是即时的，不用重跑。
const sliceTicket = latest();

export async function loadSlices() {
  if (!refs.sliceHost) return;
  const alive = refs.alive;
  const fresh = sliceTicket();
  S.sliceBusy = true;
  S.sliceErr = null;
  drawSlices();
  const wins = S.win2 ? [WIN_HEAD, S.win2] : [WIN_HEAD];
  try {
    const slices = await api.batchSlices(S.runId, wins);
    if (!alive() || !fresh()) return;
    S.slices = slices;
    S.tMax = maxT(S.slices.rows);
    // 第二张图的默认时刻要等这一步 —— 只有拿到数据才知道它测到哪儿。
    // 所以首屏是两次请求：第一张图先出来，第二张图紧接着补上。
    if (!S.win2Resolved) {
      S.win2Resolved = true;
      const w = tailWindow(S.tMax);
      if (w) { S.win2 = w; return loadSlices(); }
    }
  } catch (err) {
    if (!alive() || !fresh()) return;
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

  // 「一根深色都没有」得说出来。图注写着「深色那截是其中可信的比例」，
  // 而这个窗口里一帧都不可信 —— 柱子整根是淡的，不说的话看起来只是颜色浅，
  // 不像是「这三个数一个都不能用」。
  const measured = d.rows.filter((r) => r.values[si]?.mean !== null);
  const noneOk = measured.length > 0 && measured.every((r) => !(r.values[si]?.ok_ratio > 0));

  // 点这两个 ⓘ 提问时，把这个窗口里每个样品的读数一起带给模型 ——
  // 「为什么这一秒全都不可信」的答案就在这几个数里，不带的话它只能把
  // 「判级分三档」这段定义换个说法再讲一遍。
  const spot = () => sliceSpot(d, si);

  const note = h('div',
    h('div.chart-caption',
      `${fmtInt(n)} 个样品　窗口内`, h('span.strong', '全部帧'),
      '都参与平均，深色那截是其中可信的比例', infoDot('ot_status', { spot })),
    missing
      ? h('div.chart-caption.dim',
          `${fmtInt(missing)} 个样品在这个窗口里没有数据`,
          missing === n ? '（整批都没测到这个时刻 —— 不是算不出来，是数据里就没有）' : '')
      : null,
    noneOk
      ? h('div.chart-caption.warn-text',
          `${win} 这个窗口里`, h('span.strong', '一帧可信的都没有'),
          '　—— 柱子是全部帧的平均，但这几个数都不该拿来下结论',
          infoDot('ot_status', { spot }))
      : null);

  return figure(title, { ctl, body, note });
}

// ------------------------------------------------------------------ 给 ⓘ 的读数
//
// 点开 ⓘ 提问时，模型能看到的除了术语表那段定义，就是这里给的东西。
// 每个函数都在**点开的那一刻**才跑（`infoDot` 收函数），所以拖完滑块再点，
// 拿到的是拖之后的数，不是渲染时固化的那一份。
//
// `scope.run_id` 是这次对比的身份 —— 后端据此去取这一次的配方和标量分布，
// 而不是「最近一次」（你可能正翻着上周那次的结果）。

/** 一个时间窗里每个样品的读数。 */
function sliceSpot(d, si) {
  const w = d.windows[si];
  return {
    title: `「不同时刻的平均膜厚」里 ${fmtWin([w.from, w.to])} 这个窗口`,
    rows: d.rows.map((r) => {
      const v = r.values[si] || {};
      return [r.label,
              v.mean === null || v.mean === undefined ? '这个窗口里没有数据'
                : `${fmtNum(v.mean, 0)} nm`,
              v.n_frames ? `${v.n_ok}/${v.n_frames} 帧可信` : '',
              v.note || ''];
    }),
    note: `这批数据每个样品各自测到的时间范围见上表；整批最晚 ${fmtNum(S.tMax, 3)} s。`,
    scope: { run_id: S.runId },
  };
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
  // 第一刀（去掉标签里重复一遍的批次前缀）对**一个**标签也成立 ——
  // `ZG1/ZG1_a` 里那个 `ZG1/` 不因为只有一根柱子就变得有信息。
  let out = raw.map((l) => {
    const i = l.indexOf('/');
    if (i < 0) return l;
    const [batch, name] = [l.slice(0, i), l.slice(i + 1)];
    return name.startsWith(batch) ? name : l;
  });

  // 第二刀要看**所有**标签，一个标签谈不上「共有」，就到此为止。
  if (raw.length < 2) return out;

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
