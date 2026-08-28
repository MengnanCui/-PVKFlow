// 极简 SVG 绘图。不引第三方库：省一个依赖，也让网页图和 Mengnan 的
// matplotlib 出图看起来是一家人——同一套序列色、2px 轴线、刻度朝内。

const PALETTE = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7','--s8','--s9','--s10','--s11','--s12'];
const NS = 'http://www.w3.org/2000/svg';

export const svgEl = (name, attrs = {}) => {
    const n = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  return n;
};

const el = svgEl;

export const seriesColor = (i) => `var(${PALETTE[i % PALETTE.length]})`;

/** 「好看」的刻度：1 / 2 / 5 × 10^n。热力图的坐标轴也用它，保持一致。 */
export function niceTicks(min, max, count = 6) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [0, 1], lo: 0, hi: 1 };
  if (min === max) { const d = Math.abs(min) || 1; min -= d * 0.5; max += d * 0.5; }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 1e-9; v += step) ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return { ticks, lo, hi, step };
}

export function tickLabel(v, step) {
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(1);
  // 取能把这个刻度值原样表示出来的最少小数位（刻度都是 step 的整数倍）
  const tol = Math.abs(step || 1) * 1e-6;
  let dec = 0;
  while (dec < 6 && Math.abs(Number(v.toFixed(dec)) - v) > tol) dec++;
  return v.toFixed(dec);
}

/**
 * 画一张 X-Y 图。
 * spec: { series: [{label, x[], y[], style}], x_label, y_label }
 * 返回一个可直接插入 DOM 的元素。
 */
export function xyChart(spec, { height = 300, onSelect = null } = {}) {
  const host = document.createElement('div');
  host.className = 'chart-host';

  const series = (spec?.series || []).filter((s) => s.x?.length && s.y?.length);
  // band = 分位数带。画在曲线下面，作为背景。
  const band = spec?.band || null;
  if (!series.length && !band) {
    host.innerHTML = '<div class="empty empty-sm"><div class="empty-text">这次处理没有返回可绘制的数据</div></div>';
    return host;
  }

  const W = 760, H = height;
  const M = { t: 14, r: 16, b: 42, l: 62 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  const scan = (xs, ys) => {
    for (let i = 0; i < xs.length; i++) {
      const x = xs[i], y = ys[i];
      if (x === null || y === null || !Number.isFinite(x) || !Number.isFinite(y)) continue;
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
  };
  for (const s of series) scan(s.x, s.y);
  if (band) { scan(band.x, band.q1); scan(band.x, band.q3); }
  if (!Number.isFinite(xmin)) { xmin = 0; xmax = 1; ymin = 0; ymax = 1; }

  let view = { xmin, xmax };
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                          'aria-label': `${spec.y_label || 'Y'} vs ${spec.x_label || 'X'}` });
  host.appendChild(svg);

  const state = { hover: null, drag: null };

  // 分位数带的中位线占了 0 号色。代表曲线从 1 号起，否则第一条跟中位线
  // 同色同粗细，图上分不出哪条是统计量哪条是样品。
  // 定义在 draw() 外面：hover 提示和图例也要用同一套颜色。
  const shift = band ? 1 : 0;

  function draw() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    // 只按当前 X 视窗重算 Y 范围，放大后曲线才撑得满
    let ylo = Infinity, yhi = -Infinity;
    for (const s of series) {
      for (let i = 0; i < s.x.length; i++) {
        const x = s.x[i], y = s.y[i];
        if (x === null || y === null || x < view.xmin || x > view.xmax) continue;
        if (!Number.isFinite(y)) continue;
        if (y < ylo) ylo = y; if (y > yhi) yhi = y;
      }
    }
    if (!Number.isFinite(ylo)) { ylo = ymin; yhi = ymax; }

    const gx = niceTicks(view.xmin, view.xmax, 6);
    const gy = niceTicks(ylo, yhi, 5);
    const sx = (v) => M.l + ((v - gx.lo) / (gx.hi - gx.lo || 1)) * iw;
    const sy = (v) => M.t + ih - ((v - gy.lo) / (gy.hi - gy.lo || 1)) * ih;

    // 网格
    const grid = el('g', { stroke: 'var(--line)', 'stroke-width': 1 });
    for (const t of gy.ticks) {
      grid.appendChild(el('line', { x1: M.l, x2: M.l + iw, y1: sy(t), y2: sy(t) }));
    }
    svg.appendChild(grid);

    // 分位数带画在最底下，作为背景
    if (band) {
      let d = '';
      const pts = [];
      for (let i = 0; i < band.x.length; i++) {
        if (band.q3[i] === null) continue;
        pts.push([sx(band.x[i]), sy(band.q3[i])]);
      }
      for (let i = band.x.length - 1; i >= 0; i--) {
        if (band.q1[i] === null) continue;
        pts.push([sx(band.x[i]), sy(band.q1[i])]);
      }
      if (pts.length > 2) {
        d = 'M' + pts.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join('L') + 'Z';
        svg.appendChild(el('path', { d, fill: seriesColor(0), 'fill-opacity': .16,
                                     stroke: 'none', 'clip-path': 'url(#plotclip)' }));
      }
      let md = '', pen = false;
      for (let i = 0; i < band.x.length; i++) {
        if (band.median[i] === null) { pen = false; continue; }
        md += (pen ? 'L' : 'M') + sx(band.x[i]).toFixed(2) + ' ' +
              sy(band.median[i]).toFixed(2) + ' ';
        pen = true;
      }
      if (md) {
        svg.appendChild(el('path', { d: md, fill: 'none', stroke: seriesColor(0),
                                     'stroke-width': 2.5, 'clip-path': 'url(#plotclip)' }));
      }
    }

    // 数据
    const groupIndex = new Map();
    for (const s of series) {
      if (s.group && !groupIndex.has(s.group)) groupIndex.set(s.group, groupIndex.size);
    }
    for (const [i, s] of series.entries()) {
      // 有分组时按组着色（最多 12 组，对齐 matplotlib 规范的 12 色）
      // s.color 显式指定时优先 —— 时刻叠加谱要按时间由浅到深，
      // 那是一条连续的色阶，不是 12 色循环。
      const color = s.color
        || (s.group ? seriesColor(groupIndex.get(s.group) + shift)
                    : seriesColor(i + shift));
      const pts = [];
      let d = '', pen = false;
      for (let k = 0; k < s.x.length; k++) {
        const x = s.x[k], y = s.y[k];
        if (x === null || y === null || !Number.isFinite(x) || !Number.isFinite(y)) { pen = false; continue; }
        const px = sx(x), py = sy(y);
        pts.push([px, py, x, y]);
        d += (pen ? 'L' : 'M') + px.toFixed(2) + ' ' + py.toFixed(2) + ' ';
        pen = true;
      }
      if (s.style !== 'scatter' && d) {
        svg.appendChild(el('path', {
          d, fill: 'none', stroke: color, 'stroke-width': 2,
          'stroke-linejoin': 'round', 'stroke-linecap': 'round',
          'clip-path': 'url(#plotclip)',
        }));
      }
      if (s.style === 'scatter' || s.style === 'line+scatter') {
        const g = el('g', { fill: s.style === 'line+scatter' ? 'var(--paper)' : color,
                            stroke: color, 'stroke-width': 2 });
        const stride = Math.max(1, Math.ceil(pts.length / 180));
        for (let k = 0; k < pts.length; k += stride) {
          g.appendChild(el('circle', { cx: pts[k][0].toFixed(2), cy: pts[k][1].toFixed(2), r: 3.4 }));
        }
        svg.appendChild(g);
      }
      s._pts = pts;
    }

    // 坐标轴：2px、刻度朝内 —— 对齐 matplotlib 规范
    const axis = el('g', { stroke: 'var(--ink-3)', 'stroke-width': 2 });
    axis.appendChild(el('line', { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ih }));
    axis.appendChild(el('line', { x1: M.l, y1: M.t + ih, x2: M.l + iw, y2: M.t + ih }));
    for (const t of gx.ticks) {
      const x = sx(t);
      if (x < M.l - 0.5 || x > M.l + iw + 0.5) continue;
      axis.appendChild(el('line', { x1: x, x2: x, y1: M.t + ih, y2: M.t + ih - 6 }));
    }
    for (const t of gy.ticks) {
      const y = sy(t);
      axis.appendChild(el('line', { x1: M.l, x2: M.l + 6, y1: y, y2: y }));
    }
    svg.appendChild(axis);

    const labels = el('g', { fill: 'var(--ink-3)', 'font-size': 11,
                             'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' });
    for (const t of gx.ticks) {
      const x = sx(t);
      if (x < M.l - 0.5 || x > M.l + iw + 0.5) continue;
      labels.appendChild(Object.assign(
        el('text', { x, y: M.t + ih + 16, 'text-anchor': 'middle' }),
        { textContent: tickLabel(t, gx.step) }));
    }
    for (const t of gy.ticks) {
      labels.appendChild(Object.assign(
        el('text', { x: M.l - 8, y: sy(t) + 3.5, 'text-anchor': 'end' }),
        { textContent: tickLabel(t, gy.step) }));
    }
    svg.appendChild(labels);

    const axisText = el('g', { fill: 'var(--ink-2)', 'font-size': 12, 'font-family': 'var(--font)' });
    if (spec.x_label) {
      axisText.appendChild(Object.assign(
        el('text', { x: M.l + iw / 2, y: H - 6, 'text-anchor': 'middle' }),
        { textContent: spec.x_label }));
    }
    if (spec.y_label) {
      axisText.appendChild(Object.assign(
        el('text', { x: 13, y: M.t + ih / 2, 'text-anchor': 'middle',
                     transform: `rotate(-90 13 ${M.t + ih / 2})` }),
        { textContent: spec.y_label }));
    }
    svg.appendChild(axisText);

    const defs = el('defs');
    const clip = el('clipPath', { id: 'plotclip' });
    clip.appendChild(el('rect', { x: M.l, y: M.t, width: iw, height: ih }));
    defs.appendChild(clip);
    svg.appendChild(defs);

    // 框选高亮
    if (state.drag) {
      const [a, b] = [state.drag.from, state.drag.to].sort((p, q) => p - q);
      svg.appendChild(el('rect', {
        x: a, y: M.t, width: Math.max(1, b - a), height: ih,
        fill: 'var(--accent)', 'fill-opacity': .1,
        stroke: 'var(--accent)', 'stroke-width': 1, 'stroke-dasharray': '3 3',
      }));
    }

    // 悬停读数
    if (state.hover) {
      const { px, items } = state.hover;
      svg.appendChild(el('line', { x1: px, x2: px, y1: M.t, y2: M.t + ih,
                                   stroke: 'var(--ink-4)', 'stroke-width': 1, 'stroke-dasharray': '3 3' }));
      for (const it of items) {
        svg.appendChild(el('circle', { cx: it.px, cy: it.py, r: 4,
                                       fill: 'var(--paper)', stroke: it.color, 'stroke-width': 2.5 }));
      }
      const lines = items.map((it) => `${it.label}  ${fmtT(it.y)}`);
      const wBox = Math.min(230, 9 + Math.max(...lines.map((s) => s.length), 8) * 6.4);
      const hBox = 18 + lines.length * 14;
      const bx = Math.min(px + 12, M.l + iw - wBox - 2);
      const by = Math.max(M.t + 2, Math.min(items[0]?.py - hBox / 2 || M.t, M.t + ih - hBox - 2));
      const g = el('g');
      g.appendChild(el('rect', { x: bx, y: by, width: wBox, height: hBox, rx: 6,
                                 fill: 'var(--paper)', stroke: 'var(--line-2)', 'stroke-width': 1 }));
      g.appendChild(Object.assign(
        el('text', { x: bx + 8, y: by + 14, 'font-size': 10.5, fill: 'var(--ink-3)',
                     'font-family': 'var(--font)' }),
        { textContent: `${spec.x_label || 'x'} = ${fmtT(state.hover.x)}` }));
      items.forEach((it, i) => {
        g.appendChild(el('rect', { x: bx + 8, y: by + 22 + i * 14, width: 8, height: 2.5, fill: it.color }));
        g.appendChild(Object.assign(
          el('text', { x: bx + 20, y: by + 26 + i * 14, 'font-size': 11, fill: 'var(--ink)',
                       'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' }),
          { textContent: `${it.label}  ${fmtT(it.y)}` }));
      });
      svg.appendChild(g);
    }

    host._scale = { sx, sy, gx, gy, M, iw, ih };
  }

  const fmtT = (v) => {
    if (v === null || v === undefined || !Number.isFinite(v)) return '—';
    const a = Math.abs(v);
    if (a === 0) return '0';
    if (a >= 1e5 || a < 1e-3) return v.toExponential(3);
    return v.toFixed(a < 1 ? 4 : a < 100 ? 3 : 2);
  };

  const toLocal = (evt) => {
    const r = svg.getBoundingClientRect();
    return ((evt.clientX - r.left) / r.width) * W;
  };

  svg.addEventListener('mousemove', (e) => {
    const px = toLocal(e);
    if (state.drag) { state.drag.to = px; draw(); return; }
    const { M: m, iw: w } = host._scale || {};
    if (!m || px < m.l || px > m.l + w) { if (state.hover) { state.hover = null; draw(); } return; }
    const items = [];
    let hoverX = null;
    for (const [i, s] of series.entries()) {
      if (!s._pts?.length) continue;
      let best = null, bd = Infinity;
      for (const p of s._pts) {
        const d = Math.abs(p[0] - px);
        if (d < bd) { bd = d; best = p; }
      }
      if (best && bd < 40) {
        items.push({ label: s.label, px: best[0], py: best[1], y: best[3],
                     color: s.color || seriesColor(i + shift) });
        hoverX = best[2];
      }
    }
    state.hover = items.length ? { px, x: hoverX, items } : null;
    draw();
  });
  svg.addEventListener('mouseleave', () => { state.hover = null; state.drag = null; draw(); });
  svg.addEventListener('mousedown', (e) => {
    const px = toLocal(e);
    state.drag = { from: px, to: px };
    state.hover = null;
    e.preventDefault();
  });
  svg.addEventListener('mouseup', () => {
    if (!state.drag) return;
    const { from, to } = state.drag;
    state.drag = null;
    if (Math.abs(to - from) > 12) {
      const { gx, M: m, iw: w } = host._scale;
      const inv = (px) => gx.lo + ((px - m.l) / w) * (gx.hi - gx.lo);
      const [a, b] = [inv(from), inv(to)].sort((p, q) => p - q);
      view = { xmin: a, xmax: b };
      onSelect?.({ xmin: a, xmax: b });
    }
    draw();
  });
  svg.addEventListener('dblclick', () => { view = { xmin, xmax }; onSelect?.(null); draw(); });
  svg.style.cursor = 'crosshair';

  draw();

  const groups = [...new Set(series.map((s) => s.group).filter(Boolean))];
  if (groups.length > 1 || (series.length > 1 && series.length <= 14 && !groups.length)) {
    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    const items = groups.length
      ? groups.map((g, i) => [g, seriesColor(i + shift)])
      : series.map((s, i) => [s.label, s.color || seriesColor(i + shift)]);
    for (const [label, color] of items) {
      const span = document.createElement('span');
      const swatch = document.createElement('i');
      swatch.style.background = color;
      span.append(swatch, document.createTextNode(label));
      legend.appendChild(span);
    }
    host.appendChild(legend);
  }

  host.reset = () => { view = { xmin, xmax }; draw(); };
  host.toSVG = () => new XMLSerializer().serializeToString(svg);
  return host;
}

/**
 * 把 N 条曲线压成一条中位数曲线 + 四分位带。
 *
 * 上千条曲线叠一张图是噪声不是图 —— 超过阈值时必须降级，
 * 而且要在图注里说明降级了，不能偷偷少画。
 *
 * 各条曲线的 x 未必对齐，所以先并到一个公共网格上。
 */
export function quantileBand(series, { gridPoints = 240 } = {}) {
  const valid = series.filter((s) => s.x?.length && s.y?.length);
  if (!valid.length) return null;

  let lo = Infinity, hi = -Infinity;
  for (const s of valid) {
    for (const v of s.x) {
      if (!Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!Number.isFinite(lo) || hi <= lo) return null;

  const grid = Array.from({ length: gridPoints },
    (_, i) => lo + (i / (gridPoints - 1)) * (hi - lo));

  // 每条曲线插值到公共网格
  const resampled = valid.map((s) => {
    const out = new Array(gridPoints);
    let k = 0;
    for (let i = 0; i < gridPoints; i++) {
      const x = grid[i];
      while (k < s.x.length - 2 && s.x[k + 1] < x) k++;
      const x0 = s.x[k], x1 = s.x[k + 1];
      const y0 = s.y[k], y1 = s.y[k + 1];
      if (x < s.x[0] || x > s.x[s.x.length - 1] ||
          y0 === null || y1 === null || !Number.isFinite(y0) || !Number.isFinite(y1)) {
        out[i] = null;
      } else {
        const w = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
        out[i] = y0 + (y1 - y0) * w;
      }
    }
    return out;
  });

  const median = new Array(gridPoints);
  const q1 = new Array(gridPoints);
  const q3 = new Array(gridPoints);
  for (let i = 0; i < gridPoints; i++) {
    const col = [];
    for (const r of resampled) if (r[i] !== null) col.push(r[i]);
    if (!col.length) { median[i] = q1[i] = q3[i] = null; continue; }
    col.sort((a, b) => a - b);
    const at = (p) => col[Math.min(col.length - 1, Math.max(0,
      Math.round(p * (col.length - 1))))];
    median[i] = at(0.5); q1[i] = at(0.25); q3[i] = at(0.75);
  }
  return { x: grid, median, q1, q3, n: valid.length };
}


/**
 * 分组柱状图。一组 = 一个样品，一根柱 = 一个时间窗。
 *
 * 为什么另写一个而不是拿 xyChart 凑：横轴是**类别**不是数轴。用散点图冒充的话
 * 样品名放不下、也没法把同一个样品的两根柱挨在一起 —— 而「挨在一起」正是
 * 「这个样品 1 秒和 28 秒差多少」这件事唯一好读的画法。
 *
 * `groups`: `[{ label, values: [{ value, ratio, note }] }]`
 *   - `value` 为 null 时那根柱不画，位置留白 + 一个小小的「—」，
 *     不画成 0：0 nm 和「没测到」是两回事
 *   - `ratio` 是可信比例，画成柱子上的实心部分。整根淡、可信那截深 ——
 *     一眼看得出这个数有多少是靠得住的
 * `seriesLabels`: 每根柱的名字（时间窗），进图例
 */
export function barChart({ groups = [], seriesLabels = [], yLabel = '', unit = '' },
                         { height = 320 } = {}) {
  const host = document.createElement('div');
  host.className = 'chart-host';
  if (!groups.length || !seriesLabels.length) {
    host.innerHTML = '<div class="empty empty-sm"><div class="empty-text">'
      + '没有可对比的数据</div></div>';
    return host;
  }

  const W = 760, H = height;
  const M = { t: 14, r: 16, b: 64, l: 68 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  const all = groups.flatMap((g) => g.values.map((v) => v?.value))
    .filter((v) => Number.isFinite(v));
  const ymax = all.length ? Math.max(...all) : 1;
  const gy = niceTicks(0, ymax, 5);
  const top = Math.max(gy.hi, ymax) || 1;
  const sy = (v) => M.t + ih - (v / top) * ih;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                          preserveAspectRatio: 'xMidYMid meet' });

  // 横向网格线。柱状图比折线更需要它 —— 比高度全靠这几条线
  const grid = el('g', { stroke: 'var(--line)', 'stroke-width': 1 });
  for (const t of gy.ticks) {
    grid.appendChild(el('line', { x1: M.l, x2: M.l + iw, y1: sy(t), y2: sy(t) }));
  }
  svg.appendChild(grid);

  const nS = seriesLabels.length;
  const gw = iw / groups.length;
  const pad = Math.min(14, gw * 0.16);
  const bw = Math.max(3, (gw - pad * 2) / nS - 3);

  groups.forEach((g, gi) => {
    const x0 = M.l + gi * gw + pad;
    g.values.forEach((v, si) => {
      const x = x0 + si * (bw + 3);
      if (!Number.isFinite(v?.value)) {
        // 没有数据的位置：一个淡淡的「—」。画成 0 高的柱子会被读成「测出来是 0」
        const dash = Object.assign(
          el('text', { x: x + bw / 2, y: M.t + ih - 6, 'text-anchor': 'middle',
                       fill: 'var(--ink-4)', 'font-size': 13 }),
          { textContent: '—' });
        dash.appendChild(Object.assign(el('title'), {
          textContent: `${g.label} · ${seriesLabels[si]}：${v?.note || '没有数据'}` }));
        svg.appendChild(dash);
        return;
      }
      const yTop = sy(v.value);
      const hAll = M.t + ih - yTop;
      const color = seriesColor(si);

      // 整根：淡色 = 全部帧的均值
      const bar = el('rect', { x, y: yTop, width: bw, height: hAll,
                               fill: color, opacity: 0.32, rx: 2 });
      svg.appendChild(bar);
      // 可信那一截：深色实心，高度按可信比例
      const r = Number.isFinite(v.ratio) ? Math.max(0, Math.min(1, v.ratio)) : 0;
      if (r > 0) {
        svg.appendChild(el('rect', {
          x, y: M.t + ih - hAll * r, width: bw, height: hAll * r,
          fill: color, rx: 2 }));
      }
      bar.appendChild(Object.assign(el('title'), {
        textContent: `${g.label} · ${seriesLabels[si]}\n`
          + `${v.value} ${unit}（${v.n_frames ?? '?'} 帧的平均）\n`
          + `其中 ${v.n_ok ?? 0} 帧可信（${Math.round(r * 100)}%）` }));
    });

    // 组名。样品名很长，斜着放，放不下就截断（tooltip 里给全名）
    const label = String(g.label);
    const short = label.length > 16 ? label.slice(0, 15) + '…' : label;
    const tx = M.l + gi * gw + gw / 2;
    const txt = Object.assign(
      el('text', { x: tx, y: M.t + ih + 14, 'text-anchor': 'end',
                   fill: 'var(--ink-2)', 'font-size': 11,
                   transform: `rotate(-28 ${tx} ${M.t + ih + 14})` }),
      { textContent: short });
    txt.appendChild(Object.assign(el('title'), { textContent: label }));
    svg.appendChild(txt);
  });

  const axis = el('g', { stroke: 'var(--ink-3)', 'stroke-width': 2 });
  axis.appendChild(el('line', { x1: M.l, y1: M.t, x2: M.l, y2: M.t + ih }));
  axis.appendChild(el('line', { x1: M.l, y1: M.t + ih, x2: M.l + iw, y2: M.t + ih }));
  for (const t of gy.ticks) {
    axis.appendChild(el('line', { x1: M.l, x2: M.l + 6, y1: sy(t), y2: sy(t) }));
  }
  svg.appendChild(axis);

  const labels = el('g', { fill: 'var(--ink-3)', 'font-size': 11,
                           'font-family': 'var(--font-mono)' });
  for (const t of gy.ticks) {
    labels.appendChild(Object.assign(
      el('text', { x: M.l - 8, y: sy(t) + 3.5, 'text-anchor': 'end' }),
      { textContent: tickLabel(t, gy.step) }));
  }
  svg.appendChild(labels);

  if (yLabel) {
    svg.appendChild(Object.assign(
      el('text', { x: 13, y: M.t + ih / 2, 'text-anchor': 'middle',
                   fill: 'var(--ink-2)', 'font-size': 12,
                   'font-family': 'var(--font)',
                   transform: `rotate(-90 13 ${M.t + ih / 2})` }),
      { textContent: yLabel }));
  }

  host.appendChild(svg);

  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  seriesLabels.forEach((name, i) => {
    const span = document.createElement('span');
    span.innerHTML = `<i style="background:${seriesColor(i)}"></i>`;
    span.appendChild(document.createTextNode(name));
    legend.appendChild(span);
  });
  const hint = document.createElement('span');
  hint.className = 'dim';
  hint.textContent = '深色 = 其中可信的比例';
  legend.appendChild(hint);
  host.appendChild(legend);
  return host;
}
