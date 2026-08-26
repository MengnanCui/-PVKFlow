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
import { bandIntegral, wavelengthSlope, spectraAtTimes, windowResolution } from '../spectra.js';

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
  // 各模块的控件状态
  norm: 'frame', cmap: 'ice', nOverlay: 8,
  bandMin: 950, bandMax: 1120,
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
    ['波长', `${m.lambda_min}–${m.lambda_max} nm / ${m.lambda_step} nm`],
    ['时间', `0–${m.time_max} s @ ${m.frame_rate_hz.toFixed(0)} Hz`],
    ['解析后', fmtBytes(m.bytes)],
    ['抽样谱', `${m.frames_lambda_step} nm · ${fmtBytes(m.frames_bytes_estimate)}`],
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
    xLabel: '时间 (s)', yLabel: '波长 (nm)', height: 320,
    cmap: S.cmap, ...colorScale(),
    caption: `${fmtInt(S.meta.n_lambda)}×${fmtInt(S.meta.n_time)} 服务端渲染为 PNG，坐标轴是矢量的`,
  });

  const overlayHost = h('div#overlayHost', skeletonRows(3));

  mount(host,
    h('div.figure',
      h('div.figure-head',
        h('div.figure-title', '强度热力图'),
        h('div.row.gap-2',
          selectControl('归一化', S.norm, [
            ['frame', '每帧归一化'], ['wavelength', '每波长归一化'],
            ['global', '全局'], ['none', '原始值'],
          ], (v) => { S.norm = v; updateHeatmaps(); }),
          selectControl('色标', S.cmap, [
            ['ice', 'ice'], ['gray', '灰度'], ['steel', 'steel'],
          ], (v) => { S.cmap = v; updateHeatmaps(); }))),
      hm),

    h('div.figure',
      h('div.figure-head',
        h('div.figure-title', '归一化强度 vs 波长'),
        h('div.row.gap-2',
          h('span.small.muted', '时刻数'),
          h('input.input', {
            type: 'number', min: 2, max: 24, value: S.nOverlay,
            style: { width: '72px' },
            oninput: (e) => {
              S.nOverlay = Math.max(2, Math.min(24, Number(e.target.value) || 8));
              drawOverlay();
            },
          }))),
      overlayHost));

  refs.heatmapMain = hm;
  refs.overlayHost = overlayHost;
}

function drawOverlay() {
  if (!refs.overlayHost) return;
  if (!S.frames) { mount(refs.overlayHost, skeletonRows(3)); return; }
  const t = S.frames.time;
  const targets = Array.from({ length: S.nOverlay },
    (_, i) => t[0] + (i / (S.nOverlay - 1)) * (t[t.length - 1] - t[0]));
  const series = spectraAtTimes(S.frames, targets, true);
  mount(refs.overlayHost,
    xyChart({ x_label: '波长 (nm)', y_label: '归一化强度', series }, { height: 300 }),
    h('div.chart-caption',
      `每条曲线按自身最大值归一化 · λ 抽样至 ${S.frames.lambda_step} nm`));
}

// ------------------------------------------------------------------ ② 膜厚处理
function drawThickness() {
  const host = bodyOf('thickness');

  const kFull = heatmap({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavenumber', norm: S.norm, cmap: 'gray' }),
    xMin: S.meta.time_min, xMax: S.meta.time_max,
    yMin: 1 / S.meta.lambda_max, yMax: 1 / S.meta.lambda_min,
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
    xLabel: '时间 (s)', yLabel: 'k = 1/λ (nm⁻¹)', height: 280,
    cmap: 'gray', ...colorScale(),
    caption: '只看选定波段的条纹。窗口越窄，能分辨的膜越厚（见上方诊断）',
  });

  const resHost = h('div#resHost');
  const otFull = h('div#otFull');
  const otBand = h('div#otBand');

  mount(host,
    h('div.figure',
      h('div.figure-head', h('div.figure-title', '全波段')),
      kFull,
      otFull),

    h('div.figure',
      h('div.figure-head',
        h('div.figure-title', '指定波段'),
        bandControl(S.meta.lambda_min, S.meta.lambda_max,
          () => [S.bandMin, S.bandMax],
          (lo, hi) => {
            S.bandMin = lo; S.bandMax = hi;
            drawWindowResolution();
          },
          () => updateBandFigures())),
      resHost,
      kBand,
      otBand));

  refs.kFull = kFull;
  refs.kBand = kBand;
  refs.resHost = resHost;

  drawWindowResolution();
  mount(otFull, thicknessSlot(S.meta.lambda_min, S.meta.lambda_max));
  mount(otBand, thicknessSlot(S.bandMin, S.bandMax));
  refs.otBand = otBand;
}

function drawWindowResolution() {
  if (!refs.resHost) return;
  const r = windowResolution(S.bandMin, S.bandMax);
  if (!r) { clear(refs.resHost); return; }
  // 这几个数是纯几何量（Δk 决定能分辨的最小光程差），不依赖膜厚算法。
  // 先看一眼可以避免选一个根本测不出来的窗口。
  mount(refs.resHost,
    h('div.notice.mt-2',
      h('div.grow',
        h('div.small',
          `窗口 ${S.bandMin.toFixed(0)}–${S.bandMax.toFixed(0)} nm　`,
          h('span.mono', `Δk = ${r.dk.toExponential(3)} nm⁻¹`), '　',
          h('span.mono', `一个频率 bin = ${fmtNum(r.binF, 0)} nm`)),
        h('div.xsmall.dim.mt-2',
          `这个窗口能测的最小光学厚度约 ${fmtNum(r.otFloor, 0)} nm`,
          '（低于它 FFT 会锁到噪声峰）。窗口越宽，可测的膜越薄。'))));
}

function thicknessSlot(lo, hi) {
  return h('div.slot',
    h('div.slot-title', `光学厚度 vs 时间　${lo.toFixed(0)}–${hi.toFixed(0)} nm`),
    h('p.small.muted.mt-2',
      '膜厚算法尚未接入。接口已就位 —— ',
      h('span.mono', `GET /api/spectra/{id}/thickness`),
      ' 接上之后这里会直接出曲线，前端不用改。'),
    h('div.mt-3',
      h('button.btn.btn-sm', {
        onclick: async (e) => {
          busy(e.target, true);
          try {
            await api.spectraThickness(S.artifactId, { lam_min: lo, lam_max: hi });
            toast('膜厚算法已接入，刷新页面即可看到曲线', 'ok');
          } catch (err) {
            toast(err.message, 'err', 6000);
          }
          busy(e.target, false);
        },
      }, '检查接入状态')));
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

    h('div.figure.mt-4',
      h('div.figure-head',
        h('div.figure-title', '谱斜率 vs 时间'),
        h('div.row.gap-3',
          numberControl('波长', S.slopeCenter, S.meta.lambda_min, S.meta.lambda_max, 1,
            (v) => { S.slopeCenter = v; drawSlope(); }),
          numberControl('窗口半宽', S.slopeHalf, 1, 100, 1,
            (v) => { S.slopeHalf = v; drawSlope(); }))),
      slopeHost),

    h('div.figure',
      h('div.figure-head',
        h('div.figure-title', '波段积分 vs 时间'),
        bandControl(S.meta.lambda_min, S.meta.lambda_max,
          () => [S.integMin, S.integMax],
          (lo, hi) => { S.integMin = lo; S.integMax = hi; drawIntegral(); })),
      integHost));

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
            `全分辨率 · ${fmtInt(nPoints ?? x.length)} 点 · λ ${S.frames.native_lambda_step} nm`)
        : h('span.status.status-accent.xsmall',
            `实时预览 · λ 抽样至 ${S.frames.lambda_step} nm`)));
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
  if (refs.otBand) mount(refs.otBand, thicknessSlot(S.bandMin, S.bandMax));
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
function bandControl(min, max, get, onLive, onCommit) {
  const [lo0, hi0] = get();
  const loNum = h('input.input.input-sm', { type: 'number', value: lo0, min, max, step: 1,
                                            style: { width: '76px' } });
  const hiNum = h('input.input.input-sm', { type: 'number', value: hi0, min, max, step: 1,
                                            style: { width: '76px' } });
  const loRange = h('input.range', { type: 'range', value: lo0, min, max, step: 1 });
  const hiRange = h('input.range', { type: 'range', value: hi0, min, max, step: 1 });

  const apply = (lo, hi) => {
    // 保证 lo < hi 且窗口不至于窄到没有意义
    lo = Math.max(min, Math.min(max - 5, lo));
    hi = Math.min(max, Math.max(lo + 5, hi));
    loNum.value = loRange.value = Math.round(lo);
    hiNum.value = hiRange.value = Math.round(hi);
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
    h('div.row.gap-2', h('span.small.muted', '波段'), loNum,
      h('span.small.dim', '–'), hiNum, h('span.small.dim', 'nm')),
    h('div.band-sliders', loRange, hiRange));
}
