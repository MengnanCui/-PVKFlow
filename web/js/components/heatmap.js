// 热力图 / 条纹图：位图数据 + 矢量坐标轴。
//
// 图本身是服务端渲染的 PNG —— 10⁶ 个 SVG 矩形会卡死浏览器，而同一张图
// PNG 只要几百 KB。但坐标轴、刻度、悬停十字线用矢量画在图外面，
// 这样文字清晰、跟随明暗主题、缩放不糊。

import { h, mount } from '../ui.js';
import { niceTicks, tickLabel, svgEl as el } from '../chart.js';

/**
 * heatmap({src, xMin, xMax, yMin, yMax, xLabel, yLabel, height, ...})
 * 返回一个可插入 DOM 的元素，带 .update(newProps) 用于换参数时重画。
 */
export function heatmap(opts) {
  const host = h('div.heatmap');
  let props = { height: 300, xLabel: '时间 (s)', yLabel: '', cmapLabel: '', ...opts };
  let state = { loading: true, error: null, hover: null };

  const M = { t: 10, r: 96, b: 40, l: 62 };   // 右边留给色标条 + 数值 + 说明
  const W = 900;

  function draw() {
    const H = props.height + M.t + M.b;
    const iw = W - M.l - M.r;
    const ih = props.height;

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                            'aria-label': `${props.yLabel} 随 ${props.xLabel} 的变化` });
    svg.style.width = '100%';
    svg.style.height = 'auto';
    svg.style.display = 'block';

    // --- 位图 ---
    if (!state.error) {
      const img = el('image', {
        x: M.l, y: M.t, width: iw, height: ih,
        preserveAspectRatio: 'none',          // 两个轴是不同物理量，不保持长宽比
        href: props.src,
      });
      img.setAttribute('image-rendering', 'auto');
      svg.appendChild(img);
    }
    svg.appendChild(el('rect', { x: M.l, y: M.t, width: iw, height: ih,
                                 fill: 'none', stroke: 'var(--ink-3)', 'stroke-width': 2 }));

    // --- 坐标轴。刻度朝内，2px —— 和 matplotlib 规范一致 ---
    const gx = niceTicks(props.xMin, props.xMax, 6);
    const gy = niceTicks(props.yMin, props.yMax, 5);
    const sx = (v) => M.l + ((v - props.xMin) / (props.xMax - props.xMin || 1)) * iw;
    const sy = (v) => M.t + ih - ((v - props.yMin) / (props.yMax - props.yMin || 1)) * ih;

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
    svg.appendChild(axis);
    svg.appendChild(labels);

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
    svg.appendChild(axisText);

    // --- 色标条：不说清楚颜色对应什么数值，热力图就只是好看而已 ---
    if (props.cmap) {
      const bx = M.l + iw + 14, bw = 12;
      const gid = `cbar-${props.cmap}`;
      const defs = el('defs');
      const grad = el('linearGradient', { id: gid, x1: 0, y1: 1, x2: 0, y2: 0 });
      for (const [off, color] of RAMPS[props.cmap] || RAMPS.gray) {
        grad.appendChild(el('stop', { offset: off, 'stop-color': color }));
      }
      defs.appendChild(grad);
      svg.appendChild(defs);
      svg.appendChild(el('rect', { x: bx, y: M.t, width: bw, height: ih,
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
      svg.appendChild(cap);
    }

    // --- 悬停十字线与坐标读数 ---
    if (state.hover && !state.error) {
      const { px, py, xv, yv } = state.hover;
      const g = el('g', { stroke: 'var(--paper)', 'stroke-width': 1, opacity: .85 });
      g.appendChild(el('line', { x1: px, x2: px, y1: M.t, y2: M.t + ih }));
      g.appendChild(el('line', { x1: M.l, x2: M.l + iw, y1: py, y2: py }));
      svg.appendChild(g);

      const text = `${fmt(xv)} s , ${fmt(yv)}`;
      const bw = 18 + text.length * 6.6;
      const bx = Math.min(px + 10, M.l + iw - bw - 2);
      const by = Math.max(M.t + 2, py - 26);
      svg.appendChild(el('rect', { x: bx, y: by, width: bw, height: 20, rx: 5,
                                   fill: 'var(--paper)', stroke: 'var(--line-2)' }));
      svg.appendChild(Object.assign(
        el('text', { x: bx + 9, y: by + 14, 'font-size': 11, fill: 'var(--ink)',
                     'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' }),
        { textContent: text }));
    }

    if (state.loading && !state.error) {
      svg.appendChild(Object.assign(
        el('text', { x: M.l + iw / 2, y: M.t + ih / 2, 'text-anchor': 'middle',
                     'font-size': 12, fill: 'var(--ink-3)', 'font-family': 'var(--font)' }),
        { textContent: '正在渲染…' }));
    }

    svg.addEventListener('mousemove', (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const py = ((e.clientY - r.top) / r.height) * H;
      if (px < M.l || px > M.l + iw || py < M.t || py > M.t + ih) {
        if (state.hover) { state.hover = null; draw(); }
        return;
      }
      state.hover = {
        px, py,
        xv: props.xMin + ((px - M.l) / iw) * (props.xMax - props.xMin),
        yv: props.yMin + ((M.t + ih - py) / ih) * (props.yMax - props.yMin),
      };
      draw();
    });
    svg.addEventListener('mouseleave', () => { if (state.hover) { state.hover = null; draw(); } });
    svg.style.cursor = 'crosshair';

    mount(host,
      state.error
        ? h('div.notice.notice-warn', h('div.grow', state.error))
        : svg,
      props.caption ? h('div.chart-caption', props.caption) : null);
  }

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
    props = { ...props, ...next };
    if (next.src !== undefined) loadImage();
    else draw();
  };

  loadImage();
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
