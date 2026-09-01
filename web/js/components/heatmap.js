// 热力图 / 条纹图：位图数据 + 矢量坐标轴。
//
// 图本身是服务端渲染的 PNG —— 10⁶ 个 SVG 矩形会卡死浏览器，而同一张图
// PNG 只要几百 KB。但坐标轴、刻度、悬停十字线用矢量画在图外面，
// 这样文字清晰、跟随明暗主题、缩放不糊。

import { h, mount } from '../ui.js';
import { niceTicks, tickCount, tickLabel, svgEl as el, trackWidth } from '../chart.js';

/**
 * heatmap({src, xMin, xMax, yMin, yMax, xLabel, yLabel, height, ...})
 * 返回一个可插入 DOM 的元素，带 .update(newProps) 用于换参数时重画。
 */
// 同页多张热力图时，色标渐变的 id 不能撞 —— 撞了后挂载的会把先挂载的染成别的色标。
let hmUid = 0;

export function heatmap(opts) {
  const host = h('div.heatmap');
  let props = { height: 300, xLabel: '时间 (s)', yLabel: '', cmapLabel: '', ...opts };
  let state = { loading: true, error: null, hover: null };

  // 右边留给色标条 + 数值 + 说明。
  // 96 是当年在 900 单位的 viewBox 里定的 —— 那时候它被 0.59 倍缩放，
  // 落到屏幕上其实只有 57px。现在坐标系是真实像素，照抄 96 等于把色标区
  // 凭空放大七成、从图里抢走 40px。按实际需要重算：
  //   色标条 12 + 间隙 4 + 数值标签约 30 + 竖排说明约 21 = 67，留一点余量。
  const M = { t: 10, r: 78, b: 40, l: 62 };
  // ★ 和 chart.js 同一条规矩：绘图坐标系 = CSS 像素。
  // 以前写死 900，而容器只有 530px 宽 —— 0.59 倍缩放，11px 的刻度
  // 实际渲染成 6.5px，是全平台最小的字。
  let W = opts.width || 900;
  const uid = ++hmUid;

  // ── 常驻结构。**位图节点绝不能跟着指针重建。**
  //
  // 以前 draw() 每次都新建一个 <svg>（连里面那个 <image> 一起）再 mount 进 host，
  // 而 draw() 挂在 mousemove 上 —— 你把鼠标划过条纹图，就是在让浏览器
  // 每一帧丢掉再重建那张位图节点。副作用不只是慢：正在悬停的那个节点被换掉了，
  // 所以量它的时候第二个事件根本没有落点。
  //
  // 现在 svg / <image> / 坐标轴 / 色标常驻，只有十字线和读数跟着指针走。
  const svg = el('svg', { role: 'img' });
  svg.style.width = '100%';
  svg.style.height = 'auto';
  svg.style.display = 'block';
  svg.style.cursor = 'crosshair';
  const defs = el('defs');
  const structRoot = el('g', { class: 'heatmap-struct' });
  const overlayRoot = el('g', { class: 'heatmap-overlay' });
  svg.append(defs, structRoot, overlayRoot);

  const captionEl = h('div.chart-caption');
  let geom = null;                 // 浮层要用的几何量，骨架画完才有

  /** 骨架：位图、边框、坐标轴、刻度、色标条。数据或参数变了才重画。 */
  function drawStructure() {
    while (structRoot.firstChild) structRoot.removeChild(structRoot.firstChild);
    while (defs.firstChild) defs.removeChild(defs.firstChild);

    const H = props.height + M.t + M.b;
    const iw = W - M.l - M.r;
    const ih = props.height;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('aria-label', `${props.yLabel} 随 ${props.xLabel} 的变化`);

    const sx = (v) => M.l + ((v - props.xMin) / (props.xMax - props.xMin || 1)) * iw;
    const sy = (v) => M.t + ih - ((v - props.yMin) / (props.yMax - props.yMin || 1)) * ih;
    geom = { H, iw, ih, sx, sy };

    // --- 位图 ---
    if (!state.error) {
      const img = el('image', {
        x: M.l, y: M.t, width: iw, height: ih,
        preserveAspectRatio: 'none',          // 两个轴是不同物理量，不保持长宽比
        href: props.src,
      });
      img.setAttribute('image-rendering', 'auto');
      structRoot.appendChild(img);
    }
    structRoot.appendChild(el('rect', { x: M.l, y: M.t, width: iw, height: ih,
                                        fill: 'none', stroke: 'var(--ink-3)', 'stroke-width': 2 }));

    // --- 坐标轴。刻度朝内，2px —— 和 matplotlib 规范一致 ---
    const gx = niceTicks(props.xMin, props.xMax, tickCount(iw, 130));
    const gy = niceTicks(props.yMin, props.yMax, tickCount(ih, 60, 5, 8));

    const axis = el('g', { stroke: 'var(--ink-3)', 'stroke-width': 2 });
    const labels = el('g', { fill: 'var(--ink-3)', 'font-size': 11,
                             'font-family': 'var(--font)',
                             'font-variant-numeric': 'tabular-nums' });
    for (const v of gx.ticks) {
      const x = sx(v);
      if (x < M.l - 0.5 || x > M.l + iw + 0.5) continue;
      axis.appendChild(el('line', { x1: x, x2: x, y1: M.t + ih, y2: M.t + ih - 6 }));
      labels.appendChild(Object.assign(
        el('text', { x, y: M.t + ih + 16, 'text-anchor': 'middle' }),
        { textContent: tickLabel(v, gx.step) }));
    }
    for (const v of gy.ticks) {
      const y = sy(v);
      if (y < M.t - 0.5 || y > M.t + ih + 0.5) continue;
      axis.appendChild(el('line', { x1: M.l, x2: M.l + 6, y1: y, y2: y }));
      labels.appendChild(Object.assign(
        el('text', { x: M.l - 8, y: y + 3.5, 'text-anchor': 'end' }),
        { textContent: tickLabel(v, gy.step) }));
    }
    structRoot.appendChild(axis);
    structRoot.appendChild(labels);

    const axisText = el('g', { fill: 'var(--ink-2)', 'font-size': 12, 'font-family': 'var(--font)' });
    axisText.appendChild(Object.assign(
      el('text', { x: M.l + iw / 2, y: H - 4, 'text-anchor': 'middle' }),
      { textContent: props.xLabel }));
    if (props.yLabel) {
      axisText.appendChild(Object.assign(
        el('text', { x: 13, y: M.t + ih / 2, 'text-anchor': 'middle',
                     transform: `rotate(-90 13 ${M.t + ih / 2})` }),
        { textContent: props.yLabel }));
    }
    structRoot.appendChild(axisText);

    // --- 色标条：不说清楚颜色对应什么数值，热力图就只是好看而已 ---
    if (props.cmap) {
      const bx = M.l + iw + 14, bw = 12;
      const gid = `cbar-${props.cmap}-${uid}`;
      const grad = el('linearGradient', { id: gid, x1: 0, y1: 1, x2: 0, y2: 0 });
      for (const [off, color] of RAMPS[props.cmap] || RAMPS.gray) {
        grad.appendChild(el('stop', { offset: off, 'stop-color': color }));
      }
      defs.appendChild(grad);
      structRoot.appendChild(el('rect', { x: bx, y: M.t, width: bw, height: ih,
                                          fill: `url(#${gid})`,
                                          stroke: 'var(--ink-3)', 'stroke-width': 1 }));
      const cap = el('g', { fill: 'var(--ink-3)', 'font-size': 10,
                            'font-family': 'var(--font)',
                            'font-variant-numeric': 'tabular-nums' });
      cap.appendChild(Object.assign(
        el('text', { x: bx + bw + 4, y: M.t + 8 }),
        { textContent: fmtV(props.vMax ?? 1) }));
      cap.appendChild(Object.assign(
        el('text', { x: bx + bw + 4, y: M.t + ih }),
        { textContent: fmtV(props.vMin ?? 0) }));
      if (props.vLabel) {
        // 放在数值标签右侧，别压在色标条上
        const lx = bx + bw + 34;
        cap.appendChild(Object.assign(
          el('text', { x: lx, y: M.t + ih / 2, 'text-anchor': 'middle', fill: 'var(--ink-4)',
                       transform: `rotate(-90 ${lx} ${M.t + ih / 2})` }),
          { textContent: props.vLabel }));
      }
      structRoot.appendChild(cap);
    }

    if (state.loading && !state.error) {
      structRoot.appendChild(Object.assign(
        el('text', { x: M.l + iw / 2, y: M.t + ih / 2, 'text-anchor': 'middle',
                     'font-size': 12, fill: 'var(--ink-3)', 'font-family': 'var(--font)' }),
        { textContent: '正在渲染…' }));
    }
  }

  /** 浮层：十字线与坐标读数。只有这几个节点跟着指针走。 */
  function drawOverlay() {
    while (overlayRoot.firstChild) overlayRoot.removeChild(overlayRoot.firstChild);
    if (!state.hover || state.error || !geom) return;
    const { iw, ih } = geom;
    const { px, py, xv, yv } = state.hover;

    const g = el('g', { stroke: 'var(--paper)', 'stroke-width': 1, opacity: .85 });
    g.appendChild(el('line', { x1: px, x2: px, y1: M.t, y2: M.t + ih }));
    g.appendChild(el('line', { x1: M.l, x2: M.l + iw, y1: py, y2: py }));
    overlayRoot.appendChild(g);

    const text = `${fmt(xv)} s , ${fmt(yv)}`;
    const bw = 18 + text.length * 6.6;
    const bx = Math.min(px + 10, M.l + iw - bw - 2);
    const by = Math.max(M.t + 2, py - 26);
    overlayRoot.appendChild(el('rect', { x: bx, y: by, width: bw, height: 20, rx: 5,
                                         fill: 'var(--paper)', stroke: 'var(--line-2)' }));
    overlayRoot.appendChild(Object.assign(
      el('text', { x: bx + 9, y: by + 14, 'font-size': 11, fill: 'var(--ink)',
                   'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' }),
      { textContent: text }));
  }

  /** host 的孩子只在「有没有报错」这件事变了的时候才换，平时一个都不动。 */
  function syncHost() {
    const mode = state.error ? 'err' : 'svg';
    if (host.__mode !== mode) {
      host.__mode = mode;
      mount(host, state.error
        ? h('div.notice.notice-warn', h('div.grow', state.error))
        : svg, captionEl);
    }
    captionEl.textContent = props.caption || '';
    captionEl.hidden = !props.caption;
  }

  function draw() { drawStructure(); drawOverlay(); syncHost(); }

  // 指针移动用 rAF 合并，和 chart.js / virtual-list.js 同一个写法
  let raf = 0, pending = null;
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const ev = pending; pending = null;
      if (ev !== undefined) applyHover(ev);
      drawOverlay();                 // **只重画浮层**，位图和坐标轴一个节点不动
    });
  }

  function applyHover(ev) {
    if (!ev || !geom) { state.hover = null; return; }
    const { iw, ih } = geom;
    const r = svg.getBoundingClientRect();
    const px = ((ev.x - r.left) / r.width) * W;
    const py = ((ev.y - r.top) / r.height) * (geom.H);
    if (px < M.l || px > M.l + iw || py < M.t || py > M.t + ih) { state.hover = null; return; }
    state.hover = {
      px, py,
      xv: props.xMin + ((px - M.l) / iw) * (props.xMax - props.xMin),
      yv: props.yMin + ((M.t + ih - py) / ih) * (props.yMax - props.yMin),
    };
  }

  svg.addEventListener('mousemove', (e) => { pending = { x: e.clientX, y: e.clientY }; schedule(); });
  svg.addEventListener('mouseleave', () => { pending = null; state.hover = null; schedule(); });

  // 图片加载完才去掉「正在渲染」，失败要说清楚而不是留一块空白
  function loadImage() {
    state.loading = true;
    state.error = null;
    draw();
    const probe = new Image();
    probe.onload = () => { state.loading = false; draw(); };
    probe.onerror = async () => {
      state.loading = false;
      state.error = await explain(props.src);
      draw();
    };
    probe.src = props.src;
  }

  host.update = (next) => {
    // src 真的变了才重新探测图片。同样的地址再探一遍，只会白闪一次「正在渲染…」。
    const newSrc = next.src !== undefined && next.src !== props.src;
    props = { ...props, ...next };
    if (newSrc) loadImage();
    else draw();
  };

  loadImage();

  // 进文档之后量真实宽度重画。只重画骨架，浮层跟着走一遍就够。
  let sizeRaf = 0;
  trackWidth(host, (w) => {
    W = w;
    if (sizeRaf) return;
    sizeRaf = requestAnimationFrame(() => { sizeRaf = 0; drawStructure(); drawOverlay(); });
  }, opts.width || 0);

  return host;
}

/** PNG 端点报错时把后端给的人话原因取出来，而不是只显示一个破图标。 */
async function explain(url) {
  try {
    const r = await fetch(url);
    const d = await r.json();
    return d?.error?.message || `无法渲染这张图（HTTP ${r.status}）`;
  } catch {
    return '无法渲染这张图';
  }
}

// 与 app/parsers/render.py 的 COLORMAPS 对齐的 CSS 渐变锚点
const RAMPS = {
  gray:  [[0, 'rgb(0,0,0)'], [1, 'rgb(255,255,255)']],
  ice:   [[0, 'rgb(8,12,48)'], [0.167, 'rgb(18,62,122)'], [0.333, 'rgb(20,122,150)'],
          [0.5, 'rgb(64,176,140)'], [0.667, 'rgb(186,208,92)'],
          [0.833, 'rgb(248,236,152)'], [1, 'rgb(255,255,255)']],
  steel: [[0, 'rgb(9,18,30)'], [0.25, 'rgb(24,62,96)'], [0.5, 'rgb(36,112,160)'],
          [0.75, 'rgb(120,176,208)'], [1, 'rgb(240,248,252)']],
  rainbow: [[0, 'rgb(48,18,130)'], [0.143, 'rgb(34,74,200)'], [0.286, 'rgb(28,150,208)'],
            [0.429, 'rgb(42,190,150)'], [0.571, 'rgb(128,210,74)'],
            [0.714, 'rgb(226,206,52)'], [0.857, 'rgb(238,138,40)'],
            [1, 'rgb(206,46,44)']],
};

const fmtV = (v) => {
  if (!Number.isFinite(v)) return '';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e4 || a < 1e-2) return v.toExponential(1);
  return v.toFixed(a < 1 ? 2 : 1);
};

const fmt = (v) => {
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e4 || a < 1e-3) return v.toExponential(2);
  return v.toFixed(a < 1 ? 4 : a < 100 ? 2 : 1);
};
