// 样品详情 —— 三个处理模块。
//
// 渲染策略（见 app/parsers/render.py）：
//   热力图 / 条纹图  = 服务端 PNG + 前端矢量坐标轴。位图就该当位图传。
//   曲线            = 前端 SVG，可悬停、可框选、可导出、跟随明暗主题。
//   特殊处理        = **由功能模块渲染**（app/modules/builtin/special_processing）。
//                     模块只交声明，界面由平台按声明画 —— 同事加功能走的就是这条路。

import { api } from '../api.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy,
  fmtBytes, fmtInt, fmtNum,
} from '../ui.js';
import { xyChart } from '../chart.js';
import { heatmap } from '../components/heatmap.js';
import { figure, selectControl, numberControl,
         bandControl } from '../components/figure.js';
import { infoDot, withInfo } from '../components/info.js';
import { moduleView } from '../components/module-panel.js';
import { downloadMenu } from '../download.js';
import { spectraAtTimes, saturatedHead } from '../spectra.js';

export const meta = {
  id: 'sample',
  parent: 'process',
  title: '样品',
  desc: '',
};

// 页面上有哪几个模块 = 平台自带的两个 + **所有已装的功能模块**。
//
// 「特殊处理」现在就是一个功能模块（app/modules/builtin/special_processing），
// 和同事装的模块走完全同一条路 —— 平台自己吃自己的契约。
// 同事放一个模块进 workspace/modules/，它就出现在下面这个导航里，
// 这个文件一个字都不用改。
// 平台自己还手写着的只剩「光谱处理」了 —— 膜厚和特殊处理都已经是功能模块，
// 和同事装的走完全同一条路。光谱那一格和叠加谱、时间控件耦合得深，
// 等契约在真实使用里再站一站再迁。
const CORE_MODULES = [
  { id: 'spectra', name: '光谱处理' },
];

// 模块 id 里有点（`pl.demo`），直接当 DOM id 用的话 querySelector 得转义，
// 一处忘了就是一个 null。换成安全字符，省掉整类坑。
const domId = (moduleId) => 'mod-' + String(moduleId).replace(/[^a-zA-Z0-9_-]/g, '-');

/** 当前页面上的模块列表。装了几个功能模块就多几项。 */
function moduleList() {
  return [...CORE_MODULES,
          ...(S.modules || []).map((m) => ({ id: domId(m.id), name: m.name, spec: m }))];
}

const S = {
  artifactId: null, meta: null, frames: null, error: null,
  // ① 光谱处理
  norm: 'frame', cmap: 'rainbow', nOverlay: 8,
  tFrom: null, tTo: null,              // 叠加谱只看这一段时间，null = 全程
  lFrom: null, lTo: null,              // 叠加谱的波长范围，默认跳过饱和的短波端
  satNote: '',
  // ③ 及以后：全部由功能模块提供，这里存它们的声明
  modules: [],
  // 模块导航高亮：spyLock 是点击后的短暂锁，spyOff 摘掉上一次的监听
  spyLock: null, spyTimer: 0, spyRaf: 0, spyOff: null,
};

let refs = {};

export function actions(nav) {
  return [h('button.btn.btn-sm', { onclick: () => nav('process') }, '← 返回样品列表')];
}

export async function view(host, ctx) {
  // 上一次进这个页面挂的滚动监听要先摘掉 —— 一个 SPA 里翻十次样品就会
  // 攒十份监听，每次滚动都在给已经不存在的 DOM 算位置
  S.spyOff?.();
  S.spyOff = null;
  clearTimeout(S.spyTimer);
  S.spyLock = null;

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

  mount(nav, ...moduleList().map((m) => h('a.module-link', {
    href: `#module-${m.id}`,
    onclick: (e) => { e.preventDefault(); scrollTo(m.id); },
    dataset: { module: m.id },
  }, m.name)));

  mount(body, section('spectra', '光谱处理'));

  drawSpectra();
  await loadModules();      // 已装模块各占一节，接在上面两个后面
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
  // 点了就立刻高亮，不等滚动跟上 —— 而且在平滑滚动这段时间里锁住，
  // 免得中途扫过的模块把高亮抢走（滚动结束前用户看到的是一路乱跳）。
  setActive(id);
  S.spyLock = id;
  clearTimeout(S.spyTimer);
  S.spyTimer = setTimeout(() => { S.spyLock = null; syncSpy(); }, 700);
  refs.body.querySelector(`#module-${id}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function setActive(id) {
  for (const a of refs.nav.querySelectorAll('.module-link')) {
    a.classList.toggle('is-active', a.dataset.module === id);
  }
}

/**
 * 模块导航的高亮。
 *
 * 原来用的是 IntersectionObserver + rootMargin '-15% 0px -70%'，
 * 也就是「谁进了视口顶部那条 15% 的窄带谁高亮」。问题是**最后一个模块
 * 永远进不了那条带**：页面滚到底就停住了，「特殊处理」的顶还在带子下面，
 * 于是点它，高亮留在「膜厚处理」上 —— 点击和显示对不上。
 *
 * 改成直接算：取一条参考线（视口顶下方 25%），谁的顶最后一个越过这条线就是谁；
 * 滚到底了就强制算最后一个。这条规则对「最后一个模块比视口短」是天然成立的，
 * 不需要给它开特例。
 */
function syncSpy() {
  if (S.spyLock) return;
  const root = refs.body.closest('.viewport') || document.scrollingElement;
  if (!root) return;

  const line = root.getBoundingClientRect().top + root.clientHeight * 0.25;
  const atBottom = root.scrollTop + root.clientHeight >= root.scrollHeight - 4;

  const mods = moduleList();
  let active = mods[0].id;
  for (const m of mods) {
    const el = refs.body.querySelector(`#module-${m.id}`);
    if (el && el.getBoundingClientRect().top <= line) active = m.id;
  }
  // 到底了就是最后一个。短模块靠自己越不过参考线，只能靠这一条兜住。
  if (atBottom) active = mods[mods.length - 1].id;
  setActive(active);
}

function observeScroll() {
  const root = refs.body.closest('.viewport') || document.scrollingElement;
  if (!root) return;
  const onScroll = () => {
    if (S.spyRaf) return;
    S.spyRaf = requestAnimationFrame(() => { S.spyRaf = 0; syncSpy(); });
  };
  root.addEventListener('scroll', onScroll, { passive: true });
  // 图是异步画出来的，模块高度会变 —— 高度一变参考线的结论就变了
  const ro = new ResizeObserver(onScroll);
  ro.observe(refs.body);
  // 换样品时 refs.body 整个换掉，旧的监听要摘干净，不然越积越多
  S.spyOff = () => { root.removeEventListener('scroll', onScroll); ro.disconnect(); };
  syncSpy();
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
      figure(withInfo('全波长强度热力图', 'norm'), {
        head: [
          selectControl('归一化', S.norm, [
            ['frame', '每帧归一化'], ['wavelength', '每波长归一化'],
            ['global', '全局'], ['none', '原始值'],
          ], (v) => { S.norm = v; updateHeatmaps(); }),
          selectControl('色标', S.cmap, [
            ['rainbow', '彩虹'], ['ice', 'ice'], ['gray', '灰度'], ['steel', 'steel'],
          ], (v) => { S.cmap = v; updateHeatmaps(); }),
          dlHeatmap(() => api.heatmapUrl(S.artifactId,
            { axis: 'wavelength', norm: S.norm, cmap: S.cmap }), '强度热力图'),
        ],
        // 左边没有块状控件。这一行空着，好让两张图落在同一条线上
        body: hm,
      }),

      figure(withInfo('不同时刻的光谱叠加', 'overlay_n'), {
        head: [
          h('span.small.muted', '条数'),
          h('input.range', {
            type: 'range', min: 2, max: 24, value: S.nOverlay,
            style: { width: '110px' },
            oninput: (e) => { S.nOverlay = Number(e.target.value); drawOverlay(); },
          }),
          h('span.small.mono#nOverlayLabel', String(S.nOverlay)),
          dl(() => refs.overlayHost, '时刻叠加谱'),
        ],
        ctl: timeCtl,
        body: overlayHost,
      })));

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

  const spec = { x_label: '波长 (nm)', y_label: '归一化强度', series };
  refs.overlayHost.__spec = spec;      // 下载菜单从这儿取当前数据
  mount(refs.overlayHost,
    xyChart(spec, { height: 340 }),
    h('div.chart-caption',
      `${n} 条 · ${from.toFixed(2)}–${to.toFixed(2)} s · `,
      `${lo.toFixed(0)}–${hi.toFixed(0)} nm · 颜色由浅到深 = 由早到晚 · `,
      `每条在显示范围内按自身最大值归一化 · λ 抽样至 ${fmtNum(S.frames.lambda_step, 3)} nm`),
    S.satNote ? h('div.chart-caption.dim', S.satNote, infoDot('saturated_head')) : null);
}

/** 时间色阶：浅蓝 → 深蓝。u∈[0,1]，0 最早。 */
function timeShade(u) {
  const a = [188, 216, 236];      // 浅
  const b = [16, 52, 92];         // 深
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * u));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// ------------------------------------------------------------------ ③ 特殊处理
/**
 * 已装的功能模块，每个占一节。
 *
 * 平台的「特殊处理」也在这里面 —— 它就是一个模块，和同事装的走同一条路。
 * 界面全部由 moduleView 按声明渲染：面板、控件、图注、下载、ⓘ、对齐，
 * 模块作者一行前端代码都不写。
 */
async function loadModules() {
  let mods;
  try {
    mods = (await api.modules()).modules || [];
  } catch (err) {
    // 取不到模块列表不该让整页空着 —— 上面两个平台模块是好的
    mount(refs.body, ...refs.body.childNodes, errorBox(err, loadModules));
    return;
  }
  // 平台自带的排前面，同事装的排后面；各自按名字排
  S.modules = mods.sort((a, b) =>
    (a.origin === b.origin ? 0 : a.origin === 'builtin' ? -1 : 1)
    || a.name.localeCompare(b.name, 'zh'));

  // 先清掉上一轮的模块小节再加。这个函数可能被跑第二次（换样品、重载模块），
  // 只 append 不清理的话页面上会出现两份一模一样的模块。
  for (const old of refs.body.querySelectorAll('section.module[data-module-section]')) {
    old.remove();
  }
  for (const m of S.modules) {
    const sec = section(domId(m.id), m.name, m.description || '');
    sec.dataset.moduleSection = m.id;
    refs.body.appendChild(sec);
  }
  // 重建导航（现在多了几项）
  mount(refs.nav, ...moduleList().map((m) => h('a.module-link', {
    href: `#module-${m.id}`,
    onclick: (e) => { e.preventDefault(); scrollTo(m.id); },
    dataset: { module: m.id },
  }, m.name)));

  drawModules();
}

/** 把每个模块画进它自己那一节。要等抽样谱到位 —— A 档面板靠它本地实时算。 */
function drawModules() {
  if (!S.frames) return;
  for (const m of S.modules) {
    const host = bodyOf(domId(m.id));
    if (!host) continue;
    moduleView(host, m, {
      frames: S.frames,
      sampleName: S.meta?.sample_name || '样品',
      compute: (params, changed) =>
        api.moduleCompute(m.id, S.artifactId, params, changed).then((r) => r.panels),
    });
  }
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
    [refs.overlayHost].forEach((n) => {
      if (n) mount(n, errorBox(err, loadFrames));
    });
    return;
  }
  drawOverlayTimeControl();
  drawOverlay();
  // 抽样谱到位了，模块才画得出来（A 档面板要拿它在本地实时算）
  drawModules();
}

function updateHeatmaps() {
  const cs = colorScale();
  refs.heatmapMain?.update({
    src: api.heatmapUrl(S.artifactId, { axis: 'wavelength', norm: S.norm, cmap: S.cmap }),
    cmap: S.cmap, ...cs,
  });
}

/** 给一个图表宿主配下载菜单。取的都是**当前**那一份 —— 图会重画，
 *  在建按钮时捕获节点的话，重画一次就下载到旧图了。 */
function dl(hostGetter, name) {
  return downloadMenu({
    svg: () => hostGetter()?.querySelector('svg'),
    spec: () => hostGetter()?.__spec || null,
    name: `${S.meta?.sample_name || '样品'}_${name}`,
  });
}

/** 服务端渲染的热力图：图片直接取那张 PNG，不用在前端重画一遍。 */
function dlHeatmap(urlGetter, name) {
  return downloadMenu({
    svg: () => null,
    spec: () => null,
    imageUrl: urlGetter,
    name: `${S.meta?.sample_name || '样品'}_${name}`,
  });
}

