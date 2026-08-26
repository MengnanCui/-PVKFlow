// 样品详情 —— 三个处理模块。
//
// 渲染策略（见 app/parsers/render.py）：
//   热力图 / 条纹图  = 服务端 PNG + 前端矢量坐标轴。位图就该当位图传。
//   曲线            = 前端 SVG，可悬停、可框选、可导出、跟随明暗主题。
//   特殊处理        = 拿抽样谱在本地实时算，拖滑块 0 延迟。

import { api } from '../api.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy,
  fmtBytes, fmtInt, fmtNum,
} from '../ui.js';
import { xyChart } from '../chart.js';
import { heatmap } from '../components/heatmap.js';
import { bandIntegral, wavelengthSlope, spectraAtTimes, windowResolution,
         saturatedHead } from '../spectra.js';

export const meta = {
  id: 'sample',
  parent: 'process',
  title: '样品',
  desc: '',
};

const MODULES = [
  { id: 'spectra', name: '光谱处理' },
  { id: 'thickness', name: '膜厚处理' },
  { id: 'special', name: '特殊处理' },
];

const S = {
  artifactId: null, meta: null, frames: null, error: null,
  // ① 光谱处理
  norm: 'frame', cmap: 'rainbow', nOverlay: 8,
  tFrom: null, tTo: null,              // 叠加谱只看这一段时间，null = 全程
  lFrom: null, lTo: null,              // 叠加谱的波长范围，默认跳过饱和的短波端
  satNote: '',
  // ② 膜厚处理：775 避开吸收边，1120 是光谱仪上限
  bandMin: 775, bandMax: 1120,
  otFull: null, otBand: null, otReport: '',
  // ③ 特殊处理
  slopeCenter: 950, slopeHalf: 10,
  integMin: 800, integMax: 950,
};

let refs = {};

export function actions(nav) {
  return [h('button.btn.btn-sm', { onclick: () => nav('process') }, '← 返回样品列表')];
}

export async function view(host, ctx) {
  S.artifactId = ctx.arg;
  S.frames = null;
  S.error = null;
  if (!S.artifactId) {
    mount(host, empty('没有指定样品', h('button.btn.btn-sm',
      { onclick: () => ctx.nav('process') }, '回到样品列表')));
    return;
  }

  mount(host, skeletonRows(8));
  try {
    S.meta = await api.spectraMeta(S.artifactId);
  } catch (err) {
    mount(host, errorBox(err, () => view(host, ctx)));
    return;
  }

  const sample = S.meta.sample;
  document.querySelector('#pageTitle').textContent = sample?.name || S.meta.filename || '样品';
  document.querySelector('#pageDesc').textContent = [
    sample?.batch ? `批次 ${sample.batch}` : null,
    S.meta.filename,
  ].filter(Boolean).join(' · ');

  const nav = h('nav.module-nav');
  const body = h('div.module-body');
  mount(host,
    h('div.sample-head', headerFacts()),
    h('div.sample-layout', nav, body));
  refs = { host, nav, body, ctx };

  mount(nav, ...MODULES.map((m) => h('a.module-link', {
    href: `#module-${m.id}`,
    onclick: (e) => { e.preventDefault(); scrollTo(m.id); },
    dataset: { module: m.id },
  }, m.name)));

  mount(body,
    section('spectra', '光谱处理'),
    section('thickness', '膜厚处理'),
    section('special', '特殊处理'));

  drawSpectra();
  drawThickness();
  drawSpecial();
  loadFrames();
  observeScroll();
}

// ------------------------------------------------------------------ 头部
function headerFacts() {
  const m = S.meta;
  const facts = [
    ['矩阵', `${fmtInt(m.n_lambda)} × ${fmtInt(m.n_time)}`],
    ['波长', `${fmtNum(m.lambda_min, 3)}–${fmtNum(m.lambda_max, 3)} nm`
             + ` / 步长 ${fmtNum(m.lambda_step, 3)} nm`],
    ['时间', `0–${m.time_max} s @ ${m.frame_rate_hz.toFixed(0)} Hz`],
    ['解析后', fmtBytes(m.bytes)],
    ['抽样谱', `${fmtNum(m.frames_lambda_step, 3)} nm`
               + ` · ${fmtBytes(m.frames_bytes_estimate)}`],
  ];
  return h('div.facts',
    ...facts.map(([k, v]) => h('div.fact', h('div.fact-k', k), h('div.fact-v', v))),
    Object.keys(m.meta || {}).length
      ? h('details.fact-meta',
          h('summary.xsmall.muted', { style: { cursor: 'pointer' } }, '文件元数据'),
          h('div.mt-2', ...Object.entries(m.meta).map(([k, v]) =>
            h('div.xsmall.muted', h('span.mono', k), ' = ', String(v)))))
      : null);
}

const section = (id, title, note) =>
  h(`section.module#module-${id}`,
    h('div.module-head',
      h('h2', title),
      note ? h('div.section-note', note) : null),
    h(`div#body-${id}`));

const bodyOf = (id) => refs.body.querySelector(`#body-${id}`);

function scrollTo(id) {
  refs.body.querySelector(`#module-${id}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function observeScroll() {
  const links = [...refs.nav.querySelectorAll('.module-link')];
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const id = e.target.id.replace('module-', '');
      links.forEach((a) => a.classList.toggle('is-active', a.dataset.module === id));
    }
  }, { rootMargin: '-15% 0px -70% 0px' });
  MODULES.forEach((m) => {
    const el = refs.body.querySelector(`#module-${m.id}`);
    if (el) io.observe(el);
  });
}


/** 色标条的值域与说明，随归一化方式变化。 */
function colorScale() {
  switch (S.norm) {
    case 'frame':
      return { vMin: 0, vMax: 1, vLabel: '每帧归一化' };
    case 'wavelength':
      return { vMin: 0, vMax: 1, vLabel: '每波长归一化' };
    case 'global':
      return { vMin: S.meta.value_min, vMax: S.meta.value_max, vLabel: '强度' };
    default:
      return { vMin: 0, vMax: 1, vLabel: '原始值（裁剪到 0–1）' };
  }
}

// ------------------------------------------------------------------ ① 光谱处理
function drawSpectra() {
  const host = bodyOf('spectra');
  const hm = heatmap({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavelength', norm: S.norm, cmap: S.cmap }),
    xMin: S.meta.time_min, xMax: S.meta.time_max,
    yMin: S.meta.lambda_min, yMax: S.meta.lambda_max,
    xLabel: '时间 (s)', yLabel: '波长 (nm)', height: 340,
    cmap: S.cmap, ...colorScale(),
    caption: `${fmtInt(S.meta.n_lambda)}×${fmtInt(S.meta.n_time)} 服务端渲染为 PNG，坐标轴是矢量的`,
  });

  const overlayHost = h('div#overlayHost', skeletonRows(3));
  const timeCtl = h('div#overlayTime');

  mount(host,
    h('div.fig-grid-2',
      h('div.figure',
        h('div.figure-head',
          h('div.figure-title', '全波长强度热力图'),
          h('div.row.gap-2',
            selectControl('归一化', S.norm, [
              ['frame', '每帧归一化'], ['wavelength', '每波长归一化'],
              ['global', '全局'], ['none', '原始值'],
            ], (v) => { S.norm = v; updateHeatmaps(); }),
            selectControl('色标', S.cmap, [
              ['rainbow', '彩虹'], ['ice', 'ice'], ['gray', '灰度'], ['steel', 'steel'],
            ], (v) => { S.cmap = v; updateHeatmaps(); }))),
        hm),

      h('div.figure',
        h('div.figure-head',
          h('div.figure-title', '不同时刻的光谱叠加'),
          h('div.row.gap-2',
            h('span.small.muted', '条数'),
            h('input.range', {
              type: 'range', min: 2, max: 24, value: S.nOverlay,
              style: { width: '110px' },
              oninput: (e) => { S.nOverlay = Number(e.target.value); drawOverlay(); },
            }),
            h('span.small.mono#nOverlayLabel', String(S.nOverlay)))),
        timeCtl,
        overlayHost)));

  refs.heatmapMain = hm;
  refs.overlayHost = overlayHost;
  refs.overlayTime = timeCtl;
  drawOverlayTimeControl();
}

/** 时间和波长两个双端滑块 —— 端点就是数据里的真实范围，不用去别处读。 */
function drawOverlayTimeControl() {
  if (!refs.overlayTime || !S.frames) return;
  const t = S.frames.time;
  const lam = S.frames.lambda;
  const t0 = t[0], t1 = t[t.length - 1];
  const l0 = lam[0], l1 = lam[lam.length - 1];

  if (S.tFrom === null) { S.tFrom = t0; S.tTo = t1; }
  if (S.lFrom === null) {
    // 短波端有一段是仪器顶到量程的饱和噪声。它参与归一化会把真谱压扁，
    // 所以默认从它之后开始 —— 但是说出来，而且滑块能拉回去。
    const sat = saturatedHead(S.frames);
    S.lFrom = sat.lambda !== null && sat.count > 0 ? sat.lambda : l0;
    S.lTo = l1;
    S.satNote = sat.count > 0
      ? `默认从 ${S.lFrom.toFixed(0)} nm 起 —— 更短的 ${sat.count} 条波长上，`
        + '超过六成的帧顶在量程两端（光源在紫外没有输出），'
        + '算进来会把真谱压扁。想看的话把左端滑块拉回去。'
      : '';
  }

  mount(refs.overlayTime,
    h('div.row.gap-4.wrap',
      bandControl(t0, t1, () => [S.tFrom, S.tTo],
        (lo, hi) => { S.tFrom = lo; S.tTo = hi; drawOverlay(); },
        null, { label: '时间', unit: 's', step: 0.05,
                minSpan: Math.max(0.05, (t1 - t0) / 50) }),
      bandControl(l0, l1, () => [S.lFrom, S.lTo],
        (lo, hi) => { S.lFrom = lo; S.lTo = hi; drawOverlay(); },
        null, { label: '波长', unit: 'nm', step: 1, minSpan: 20 })));
}

function drawOverlay() {
  if (!refs.overlayHost) return;
  const label = refs.host?.querySelector('#nOverlayLabel');
  if (label) label.textContent = String(S.nOverlay);
  if (!S.frames) { mount(refs.overlayHost, skeletonRows(3)); return; }

  const t = S.frames.time;
  const from = S.tFrom ?? t[0];
  const to = S.tTo ?? t[t.length - 1];
  const n = S.nOverlay;
  const targets = n === 1 ? [from]
    : Array.from({ length: n }, (_, i) => from + (i / (n - 1)) * (to - from));

  // 由浅到深：浅 = 早，深 = 晚。一条连续的色阶，不是 12 色循环 ——
  // 24 条曲线用循环色的话，第 1 条和第 13 条同色，时间顺序就读不出来了。
  // 归一化在**显示范围内**做。拿全谱的最大值归一化的话，被裁掉的饱和区
  // 反而说了算，图上什么都看不见。
  const lam = S.frames.lambda;
  const lo = S.lFrom ?? lam[0], hi = S.lTo ?? lam[lam.length - 1];
  const series = spectraAtTimes(S.frames, targets, false).map((s, i) => {
    const x = [], y = [];
    let peak = -Infinity;
    for (let k = 0; k < s.x.length; k++) {
      if (s.x[k] < lo || s.x[k] > hi) continue;
      x.push(s.x[k]); y.push(s.y[k]);
      if (s.y[k] > peak) peak = s.y[k];
    }
    const norm = peak > 0 ? y.map((v) => v / peak) : y;
    return { ...s, x, y: norm, color: timeShade(n === 1 ? 1 : i / (n - 1)) };
  });

  mount(refs.overlayHost,
    xyChart({ x_label: '波长 (nm)', y_label: '归一化强度', series }, { height: 340 }),
    h('div.chart-caption',
      `${n} 条 · ${from.toFixed(2)}–${to.toFixed(2)} s · `,
      `${lo.toFixed(0)}–${hi.toFixed(0)} nm · 颜色由浅到深 = 由早到晚 · `,
      `每条在显示范围内按自身最大值归一化 · λ 抽样至 ${fmtNum(S.frames.lambda_step, 3)} nm`),
    S.satNote ? h('div.chart-caption.dim', S.satNote) : null);
}

/** 时间色阶：浅蓝 → 深蓝。u∈[0,1]，0 最早。 */
function timeShade(u) {
  const a = [188, 216, 236];      // 浅
  const b = [16, 52, 92];         // 深
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * u));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// ------------------------------------------------------------------ ② 膜厚处理
//
// 四宫格：上排全波段，下排指定波段；每排左边是条纹图，右边是 OT 曲线。
// 上下一比就知道「窗口收窄换来了什么、代价是什么」——
// 窄窗口条纹看得清，但能测的最小厚度被抬高了。
function drawThickness() {
  const host = bodyOf('thickness');
  const L0 = S.meta.lambda_min, L1 = S.meta.lambda_max;

  const kFull = heatmap({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavenumber', norm: S.norm, cmap: 'gray' }),
    xMin: S.meta.time_min, xMax: S.meta.time_max,
    yMin: 1 / L1, yMax: 1 / L0,
    xLabel: '时间 (s)', yLabel: 'k = 1/λ (nm⁻¹)', height: 300,
    cmap: 'gray', ...colorScale(),
    caption: '全波段干涉条纹。相位对波数线性，所以只有在 k 轴上条纹才是等周期的',
  });

  const kBand = heatmap({
    src: api.heatmapUrl(S.artifactId, {
      axis: 'wavenumber', norm: S.norm, cmap: 'gray',
      lam_min: S.bandMin, lam_max: S.bandMax,
    }),
    xMin: S.meta.time_min, xMax: S.meta.time_max,
    yMin: 1 / S.bandMax, yMax: 1 / S.bandMin,
    xLabel: '时间 (s)', yLabel: 'k = 1/λ (nm⁻¹)', height: 300,
    cmap: 'gray', ...colorScale(),
    caption: '只看选定波段的条纹。窗口越窄，能分辨的膜越厚',
  });

  const otFullHost = h('div#otFullHost', skeletonRows(3));
  const otBandHost = h('div#otBandHost', skeletonRows(3));
  const resHost = h('div#resHost');
  const reportHost = h('div#otReport');

  mount(host,
    h('div.fig-grid-4',
      h('div.figure',
        h('div.figure-head', h('div.figure-title', `全波段条纹　${L0.toFixed(0)}–${L1.toFixed(0)} nm`)),
        kFull),
      h('div.figure',
        h('div.figure-head', h('div.figure-title', '全波段　光学厚度 vs 时间')),
        h('div.notice.notice-warn.mb-3',
          h('div.grow',
            h('div.small.strong', '这一格是对照，不是测量结果'),
            h('p.xsmall.dim.mt-2',
              '全波段跨过了吸收边，还带上了短波端没有信号的区段 —— ',
              '规范要求分析波段必须落在膜的透明区。这里画出来是为了跟下面那格比：',
              '窗口选错时曲线会崩到噪声上，而且是看得见地崩 —— 不是悄悄给个错数。'))),
        otFullHost),
      h('div.figure',
        h('div.figure-head',
          h('div.figure-title', '指定波段条纹'),
          bandControl(L0, L1, () => [S.bandMin, S.bandMax],
            (lo, hi) => { S.bandMin = lo; S.bandMax = hi; drawWindowResolution(); },
            () => { updateBandFigures(); loadThickness('band'); })),
        resHost,
        kBand),
      h('div.figure',
        h('div.figure-head', h('div.figure-title', '指定波段　光学厚度 vs 时间')),
        otBandHost)),
    reportHost);

  refs.kFull = kFull;
  refs.kBand = kBand;
  refs.resHost = resHost;
  refs.otFullHost = otFullHost;
  refs.otBandHost = otBandHost;
  refs.otReport = reportHost;

  drawWindowResolution();
  loadThickness('full');
  loadThickness('band');
}

/** 拉一条 OT 曲线。which='full' 用全波段，'band' 用当前选的窗口。 */
async function loadThickness(which) {
  const hostEl = which === 'full' ? refs.otFullHost : refs.otBandHost;
  if (!hostEl) return;
  const lo = which === 'full' ? S.meta.lambda_min : S.bandMin;
  const hi = which === 'full' ? S.meta.lambda_max : S.bandMax;

  mount(hostEl, skeletonRows(3));
  const token = (thicknessToken[which] = (thicknessToken[which] || 0) + 1);
  try {
    const d = await api.spectraThickness(S.artifactId, { lam_min: lo, lam_max: hi });
    if (token !== thicknessToken[which]) return;      // 已经有更新的请求了
    if (which === 'band') { S.otReport = d.report; drawOtReport(); }
    paintThickness(hostEl, d, lo, hi);
  } catch (err) {
    if (token !== thicknessToken[which]) return;
    mount(hostEl, errorBox(err, () => loadThickness(which)));
  }
}

const thicknessToken = {};

function paintThickness(hostEl, d, lo, hi) {
  const okX = [], okY = [], badX = [], badY = [];
  for (let i = 0; i < d.x.length; i++) {
    (d.status[i] === 'OK' ? okX : badX).push(d.x[i]);
    (d.status[i] === 'OK' ? okY : badY).push(d.y[i]);
  }

  // 规范 §6：线 alpha=0.7 + 空心散点；非 OK 的点用 COLORS[1] 标出来。
  // 不可信的点照画 —— 判据只打标志，不改数值，也不藏点。
  const series = [{
    label: `OT ${lo.toFixed(0)}–${hi.toFixed(0)} nm`,
    x: d.x, y: d.y, style: 'line', color: 'var(--s1)',
  }];
  if (badX.length) {
    series.push({ label: '不可信', x: badX, y: badY, style: 'scatter', color: 'var(--s2)' });
  }

  const nBad = d.n_points - d.n_ok;
  mount(hostEl,
    xyChart({ x_label: '时间 (s)', y_label: '光学厚度 OT = n·d·cosθ (nm)', series },
            { height: 300 }),
    h('div.chart-caption',
      `${fmtInt(d.n_points)} 帧 · `,
      nBad
        ? h('span.status.status-warn', `${fmtInt(nBad)} 帧不可信（红点）`)
        : h('span.status.status-ok', '全部可信'),
      `　可测下限 ${fmtNum(d.diagnostics.ot_floor_nm, 0)} nm`,
      `　量化格距 ${fmtNum(d.diagnostics.ot_quantum_nm, 0)} nm`));
}

/** 规范 §5 的块 A–D。写死了「禁止简化、禁止省略」，所以整段摊开，不折叠。 */
function drawOtReport() {
  if (!refs.otReport) return;
  if (!S.otReport) { clear(refs.otReport); return; }
  mount(refs.otReport,
    h('div.figure.mt-4',
      h('div.figure-head',
        h('div.figure-title', '完整报告'),
        h('span.xsmall.dim',
          'fringe-optical-thickness 规范 §5 要求块 A–D 一个都不能少')),
      h('pre.code-block', S.otReport)));
}

function drawWindowResolution() {
  if (!refs.resHost) return;
  const r = windowResolution(S.bandMin, S.bandMax);
  if (!r) { clear(refs.resHost); return; }
  // 这几个数是纯几何量（Δk 决定能分辨的最小光程差），不依赖具体数据。
  // 先看一眼可以避免选一个根本测不出来的窗口。
  mount(refs.resHost,
    h('div.notice.mb-3',
      h('div.grow',
        h('div.small',
          `窗口 ${S.bandMin.toFixed(0)}–${S.bandMax.toFixed(0)} nm　`,
          h('span.mono', `Δk = ${r.dk.toExponential(3)} nm⁻¹`), '　',
          h('span.mono', `一个频率 bin = ${fmtNum(r.binF, 0)} nm`)),
        h('div.xsmall.dim.mt-2',
          `这个窗口能测的最小光学厚度约 ${fmtNum(r.otFloor, 0)} nm`,
          '（低于它 FFT 会锁到噪声峰）。窗口越宽，可测的膜越薄。'))));
}

// ------------------------------------------------------------------ ③ 特殊处理
function drawSpecial() {
  const host = bodyOf('special');
  const slopeHost = h('div#slopeHost', skeletonRows(3));
  const integHost = h('div#integHost', skeletonRows(3));

  mount(host,
    h('p.small.muted.measure',
      '这两条曲线用「光谱处理」已经载入的抽样谱在本地实时计算 —— 拖动控件时曲线',
      '连续跟着变，不需要等后端；停下约 0.3 秒后自动换成全波长分辨率的精确结果。'),

    h('div.fig-grid-2.mt-4',
      h('div.figure',
        h('div.figure-head',
          h('div.figure-title', '谱斜率 vs 时间'),
          h('div.row.gap-3',
            numberControl('波长', S.slopeCenter, S.meta.lambda_min, S.meta.lambda_max, 1,
              (v) => { S.slopeCenter = v; drawSlope(); }),
            numberControl('半宽', S.slopeHalf, 1, 100, 1,
              (v) => { S.slopeHalf = v; drawSlope(); }))),
        slopeHost),

      h('div.figure',
        h('div.figure-head',
          h('div.figure-title', '波段积分 vs 时间'),
          bandControl(S.meta.lambda_min, S.meta.lambda_max,
            () => [S.integMin, S.integMax],
            (lo, hi) => { S.integMin = lo; S.integMax = hi; drawIntegral(); })),
        integHost)));

  refs.slopeHost = slopeHost;
  refs.integHost = integHost;
}

function drawSlope() {
  if (!refs.slopeHost || !S.frames) return;
  renderCurve(refs.slopeHost,
    wavelengthSlope(S.frames, S.slopeCenter, S.slopeHalf),
    `dI/dλ @ ${S.slopeCenter} nm`, 'dI/dλ (a.u./nm)',
    { kind: 'slope', center: S.slopeCenter, half_width: S.slopeHalf });
}

function drawIntegral() {
  if (!refs.integHost || !S.frames) return;
  renderCurve(refs.integHost,
    bandIntegral(S.frames, S.integMin, S.integMax),
    `∫ ${S.integMin}–${S.integMax} nm`, '积分强度 (a.u.·nm)',
    { kind: 'integral', lam_min: S.integMin, lam_max: S.integMax });
}

// 每个曲线容器一个防抖计时器：拖动过程中不打后端，停下来才取精确值
const pending = new WeakMap();

/**
 * 画一条派生曲线。
 *
 * 先用抽样谱在本地画（0.1 ms 级），停手后自动取后端的全分辨率版本替换。
 * 抽样带来的偏差约为信号 RMS 的 1–2%，肉眼看不出，但最终结果必须是精确的 ——
 * 所以两步都做，并且在标注上如实说明当前看到的是哪一种。
 */
function renderCurve(host, y, label, yLabel, serverParams) {
  paint(host, S.frames.time, y, label, yLabel, false);

  clearTimeout(pending.get(host));
  pending.set(host, setTimeout(async () => {
    try {
      const exact = await api.spectraCurve(S.artifactId, serverParams);
      paint(host, exact.x, exact.y, exact.label, exact.unit, true, exact.n_points);
    } catch {
      // 取不到精确值就保留预览，不打断用户
    }
  }, 300));
}

function paint(host, x, y, label, yLabel, exact, nPoints) {
  mount(host,
    xyChart({ x_label: '时间 (s)', y_label: yLabel,
              series: [{ label, x, y, style: 'line' }] }, { height: 280 }),
    h('div.chart-caption',
      exact
        ? h('span.status.status-ok.xsmall',
            `全分辨率 · ${fmtInt(nPoints ?? x.length)} 点 · λ ${fmtNum(S.frames.native_lambda_step, 3)} nm`)
        : h('span.status.status-accent.xsmall',
            `实时预览 · λ 抽样至 ${fmtNum(S.frames.lambda_step, 3)} nm`)));
}

// ------------------------------------------------------------------ 数据加载
async function loadFrames() {
  try {
    const info = await api.spectraFrames(S.artifactId);
    const flat = await api.spectraFramesBin(info.data_url);
    const [L, T] = info.shape;
    if (flat.length !== L * T) {
      throw new Error(`抽样谱长度不符：期望 ${L * T}，实际 ${flat.length}`);
    }
    // 行主序（行=波长）切成 L 个视图，不复制数据
    const values = new Array(L);
    for (let i = 0; i < L; i++) values[i] = flat.subarray(i * T, (i + 1) * T);
    S.frames = { ...info, values };
  } catch (err) {
    S.frames = null;
    [refs.overlayHost, refs.slopeHost, refs.integHost].forEach((n) => {
      if (n) mount(n, errorBox(err, loadFrames));
    });
    return;
  }
  drawOverlayTimeControl();
  drawOverlay();
  drawSlope();
  drawIntegral();
}

function updateHeatmaps() {
  const cs = colorScale();
  refs.heatmapMain?.update({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavelength', norm: S.norm, cmap: S.cmap }),
    cmap: S.cmap, ...cs,
  });
  refs.kFull?.update({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavenumber', norm: S.norm, cmap: 'gray' }),
    ...cs,
  });
  updateBandFigures();
}

function updateBandFigures() {
  refs.kBand?.update({
    src: api.heatmapUrl(S.artifactId, {
      axis: 'wavenumber', norm: S.norm, cmap: 'gray',
      lam_min: S.bandMin, lam_max: S.bandMax,
    }),
    yMin: 1 / S.bandMax, yMax: 1 / S.bandMin, ...colorScale(),
  });
}

// ------------------------------------------------------------------ 控件
function selectControl(label, value, options, onChange) {
  return h('label.inline-field',
    h('span.small.muted', label),
    h('select.select.select-sm', { onchange: (e) => onChange(e.target.value) },
      ...options.map(([v, t]) => h('option', { value: v, selected: v === value }, t))));
}

function numberControl(label, value, min, max, step, onChange) {
  const num = h('input.input.input-sm', {
    type: 'number', value, min, max, step, style: { width: '78px' },
  });
  const range = h('input.range', { type: 'range', value, min, max, step });
  const push = (v) => {
    const clamped = Math.max(min, Math.min(max, Number(v)));
    num.value = clamped;
    range.value = clamped;
    onChange(clamped);
  };
  num.oninput = (e) => push(e.target.value);
  range.oninput = (e) => push(e.target.value);       // 拖动时连续触发 —— 这才叫实时
  return h('label.inline-field', h('span.small.muted', label), num, range);
}

/**
 * 波段选择：两个滑块 + 两个数字框。
 * onLive 在拖动过程中连续触发（前端算得起），onCommit 在松手时触发（要打后端）。
 */
function bandControl(min, max, get, onLive, onCommit, opts = {}) {
  // 波长用整数步进就够；时间轴要小数，所以步长和最小跨度都可配。
  const step = opts.step ?? 1;
  const minSpan = opts.minSpan ?? 5;
  const round = (v) => (step >= 1 ? Math.round(v) : Number(v.toFixed(3)));

  const [lo0, hi0] = get();
  const numAttrs = { type: 'number', min, max, step, style: { width: '76px' } };
  const loNum = h('input.input.input-sm', { ...numAttrs, value: lo0 });
  const hiNum = h('input.input.input-sm', { ...numAttrs, value: hi0 });
  const loRange = h('input.range', { type: 'range', value: lo0, min, max, step });
  const hiRange = h('input.range', { type: 'range', value: hi0, min, max, step });

  const apply = (lo, hi) => {
    // 保证 lo < hi 且窗口不至于窄到没有意义
    lo = Math.max(min, Math.min(max - minSpan, lo));
    hi = Math.min(max, Math.max(lo + minSpan, hi));
    loNum.value = loRange.value = round(lo);
    hiNum.value = hiRange.value = round(hi);
    onLive(Number(loNum.value), Number(hiNum.value));
  };
  const readLo = (v) => apply(Number(v), Number(hiNum.value));
  const readHi = (v) => apply(Number(loNum.value), Number(v));

  loNum.oninput = (e) => readLo(e.target.value);
  hiNum.oninput = (e) => readHi(e.target.value);
  loRange.oninput = (e) => readLo(e.target.value);
  hiRange.oninput = (e) => readHi(e.target.value);
  if (onCommit) {
    // 打后端的操作等松手，别在拖动过程中发几十个请求
    loRange.onchange = onCommit;
    hiRange.onchange = onCommit;
    loNum.onchange = onCommit;
    hiNum.onchange = onCommit;
  }

  return h('div.band-control',
    h('div.row.gap-2', h('span.small.muted', opts.label ?? '波段'), loNum,
      h('span.small.dim', '–'), hiNum,
      h('span.small.dim', opts.unit ?? 'nm')),
    h('div.band-sliders', loRange, hiRange));
}
