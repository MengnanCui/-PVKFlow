// 构效关系页的两种图：散点图（每个样品一个点）、箱线图（每个批次一个箱）。
//
// 不用 chart.js 的 xyChart：那是画**曲线**的 —— 悬停按 X 找最近的点、框选只缩放 X、
// 点多了还会抽稀（每条最多画 180 个点）。散点图里一个点就是一个样品，一个都不能少，
// 悬停要找的是二维上离指针最近的那个样品。坐标轴和刻度和 xyChart 共用一份（chart.drawAxes）。

import {
  svgEl as el, niceTicks, tickCount, trackWidth, seriesColor, legendMark,
  drawGrid, drawAxes, fmtValue,
} from '../chart.js';

let uid = 0;
const HIT = 14;          // 指针离点多近算「指着它」（像素）

/** 一个读数框。lines 第一行加粗。 */
function tipBox(root, { x, y, lines, color, M, iw, ih }) {
  const w = Math.min(280, 16 + Math.max(...lines.map((s) => s.length)) * 6.6);
  const h = 10 + lines.length * 15;
  const bx = Math.min(Math.max(M.l + 2, x + 12), M.l + iw - w - 2);
  const by = Math.min(Math.max(M.t + 2, y - h - 8), M.t + ih - h - 2);
  const g = el('g');
  g.appendChild(el('rect', { x: bx, y: by, width: w, height: h, rx: 6,
                             fill: 'var(--paper)', stroke: color || 'var(--line-2)', 'stroke-width': 1 }));
  lines.forEach((t, i) => g.appendChild(Object.assign(
    el('text', { x: bx + 8, y: by + 17 + i * 15, 'font-size': i ? 11 : 11.5,
                 'font-weight': i ? 400 : 600, fill: i ? 'var(--ink-2)' : 'var(--ink)',
                 'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' }),
    { textContent: t })));
  root.appendChild(g);
}

/**
 * 散点图。
 * spec: { points: [{x, y, label, group, id}], x_label, y_label, fit: {slope, intercept} | null }
 * opts.onPick(point)：点一下某个点（打开那个样品）
 */
export function scatterChart(spec, { height = 360, onPick = null } = {}) {
  const host = document.createElement('div');
  host.className = 'chart-host';
  const pts = (spec.points || []).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (!pts.length) {
    host.innerHTML = '<div class="empty empty-sm"><div class="empty-text">没有同时有这两个数的样品</div></div>';
    return host;
  }

  const groups = [...new Set(pts.map((p) => p.group || ''))];
  const colorOf = new Map(groups.map((g, i) => [g, seriesColor(i)]));
  let W = 760;
  const H = height;
  const M = { t: 14, r: 16, b: 42, l: 62 };
  const ih = H - M.t - M.b;
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const [xmin, xmax, ymin, ymax] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];

  const svg = el('svg', { role: 'img', 'aria-label': `${spec.y_label} vs ${spec.x_label}，${pts.length} 个样品` });
  const clipId = `scatterclip-${++uid}`;
  const clipRect = el('rect', { y: M.t, height: ih });
  const clip = el('clipPath', { id: clipId });
  clip.appendChild(clipRect);
  const defs = el('defs');
  defs.appendChild(clip);
  const struct = el('g'), overlay = el('g');
  svg.append(defs, struct, overlay);
  host.appendChild(svg);
  let scale = null, hover = null;

  function drawStructure() {
    while (struct.firstChild) struct.removeChild(struct.firstChild);
    const iw = W - M.l - M.r;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    clipRect.setAttribute('x', M.l);
    clipRect.setAttribute('width', iw);
    const gx = niceTicks(xmin, xmax, tickCount(iw, 130));
    const gy = niceTicks(ymin, ymax, tickCount(ih, 60, 5, 8));
    const sx = (v) => M.l + ((v - gx.lo) / (gx.hi - gx.lo || 1)) * iw;
    const sy = (v) => M.t + ih - ((v - gy.lo) / (gy.hi - gy.lo || 1)) * ih;
    drawGrid(struct, { M, iw, gy, sy });

    // 最小二乘参考线：只是帮眼睛看趋势，不是模型
    if (spec.fit) {
      const y0 = spec.fit.intercept + spec.fit.slope * gx.lo;
      const y1 = spec.fit.intercept + spec.fit.slope * gx.hi;
      struct.appendChild(el('line', { x1: sx(gx.lo), y1: sy(y0), x2: sx(gx.hi), y2: sy(y1),
        stroke: 'var(--ink-4)', 'stroke-width': 1.5, 'stroke-dasharray': '6 4',
        'clip-path': `url(#${clipId})` }));
    }
    const dots = el('g', { 'stroke-width': 1.2, stroke: 'var(--paper)' });
    for (const p of pts) {
      p._px = sx(p.x); p._py = sy(p.y);
      dots.appendChild(el('circle', { cx: p._px.toFixed(2), cy: p._py.toFixed(2), r: 4.5,
                                      fill: colorOf.get(p.group || ''), 'fill-opacity': 0.85 }));
    }
    struct.appendChild(dots);
    drawAxes(struct, { M, iw, ih, H, gx, gy, sx, sy, xLabel: spec.x_label, yLabel: spec.y_label });
    scale = { M, iw, ih };
  }

  function drawOverlay() {
    while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
    if (!hover || !scale) return;
    const c = colorOf.get(hover.group || '');
    overlay.appendChild(el('circle', { cx: hover._px, cy: hover._py, r: 7, fill: 'none',
                                       stroke: c, 'stroke-width': 2.5 }));
    tipBox(overlay, { x: hover._px, y: hover._py, color: c, ...scale, lines: [
      hover.label, hover.group ? `批次 ${hover.group}` : '（无批次）',
      `${spec.x_label}：${fmtValue(hover.x)}`, `${spec.y_label}：${fmtValue(hover.y)}`,
      ...(onPick ? ['点一下打开这个样品'] : [])] });
  }

  const nearest = (evt) => {
    const r = svg.getBoundingClientRect();
    const px = ((evt.clientX - r.left) / r.width) * W;
    const py = ((evt.clientY - r.top) / r.height) * H;
    let best = null, bd = HIT * HIT;
    for (const p of pts) {
      const d = (p._px - px) ** 2 + (p._py - py) ** 2;
      if (d < bd) { bd = d; best = p; }
    }
    return best;
  };
  let raf = 0, pending = null;
  svg.addEventListener('mousemove', (e) => {
    pending = e;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const next = pending ? nearest(pending) : null;
      if (next !== hover) { hover = next; drawOverlay(); }
      svg.style.cursor = hover && onPick ? 'pointer' : 'default';
    });
  });
  svg.addEventListener('mouseleave', () => { pending = null; hover = null; drawOverlay(); });
  if (onPick) svg.addEventListener('click', (e) => { const p = nearest(e); if (p) onPick(p); });

  drawStructure();
  trackWidth(host, (w) => { W = w; drawStructure(); drawOverlay(); });

  // 图例：颜色只有 12 种。批次比这多时颜色会重复，一面墙似的图例反而误导 ——
  // 那时只说一句，靠悬停看是哪个样品
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  if (groups.length > 12) {
    legend.appendChild(Object.assign(document.createElement('span'), {
      className: 'small dim',
      textContent: `${groups.length} 个批次，颜色只有 12 种、会重复 —— 指到点上看是哪个样品`,
    }));
    host.appendChild(legend);
  } else if (groups.length > 1 || groups[0]) {
    for (const g of groups) {
      const span = document.createElement('span');
      span.append(legendMark(colorOf.get(g)), document.createTextNode(g || '（无批次）'));
      legend.appendChild(span);
    }
    host.appendChild(legend);
  }
  host.toSVG = () => new XMLSerializer().serializeToString(svg);
  return host;
}

/**
 * 箱线图：每组一个箱。箱 = Q1–Q3，中间那条 = 中位数，须 = 1.5 倍 IQR 以内最远的点，
 * 须外面的点单独画出来（离群点），悬停能看到是哪个样品。
 * spec: { groups: [{name, n, q1, median, q3, whisker_lo, whisker_hi, outliers: [{name, y, sample_id}]}], y_label, x_label }
 */
export function boxPlot(spec, { height = 320, onPick = null } = {}) {
  const host = document.createElement('div');
  host.className = 'chart-host';
  const groups = (spec.groups || []).filter((g) => g.n > 0);
  if (!groups.length) {
    host.innerHTML = '<div class="empty empty-sm"><div class="empty-text">这个字段在范围里没有数</div></div>';
    return host;
  }
  // 组名太长、组太多时，横着写的类别名会叠在一起：那就斜着写，并给底下多留地方
  const short = (t) => (t.length > 16 ? t.slice(0, 15) + '…' : t);
  const longest = Math.max(...groups.map((g) => short(g.name).length));
  const minW0 = 62 + 16 + groups.length * 34;
  const slant = groups.length > 6 && longest * 6.5 > 34;
  const H = height + (slant ? Math.min(90, longest * 4.6) : 0);
  const M = { t: 14, r: 16, b: slant ? 42 + Math.min(90, longest * 4.6) : 42, l: 62 };
  const ih = H - M.t - M.b;
  // 组多了每个箱至少留 34px，宽度不够就让图本身变宽、外面横向滚动 —— 不把箱挤成一条线
  const minW = minW0;
  let W = 760;
  let lo = Infinity, hi = -Infinity;
  for (const g of groups) {
    lo = Math.min(lo, g.whisker_lo, ...g.outliers.map((o) => o.y));
    hi = Math.max(hi, g.whisker_hi, ...g.outliers.map((o) => o.y));
  }

  const svg = el('svg', { role: 'img', 'aria-label': `${spec.y_label} 按${spec.x_label || '批次'}的箱线图，${groups.length} 组` });
  const struct = el('g'), overlay = el('g');
  svg.append(struct, overlay);
  const scroller = document.createElement('div');
  scroller.style.overflowX = 'auto';
  scroller.appendChild(svg);
  host.appendChild(scroller);
  let marks = [], scale = null;

  function drawStructure() {
    while (struct.firstChild) struct.removeChild(struct.firstChild);
    const w = Math.max(W, minW);
    const iw = w - M.l - M.r;
    svg.setAttribute('viewBox', `0 0 ${w} ${H}`);
    svg.style.width = `${w}px`;
    const gy = niceTicks(lo, hi, tickCount(ih, 60, 5, 8));
    const sy = (v) => M.t + ih - ((v - gy.lo) / (gy.hi - gy.lo || 1)) * ih;
    const slot = iw / groups.length;
    const bw = Math.min(46, slot * 0.55);
    drawGrid(struct, { M, iw, gy, sy });
    marks = [];
    groups.forEach((g, i) => {
      const cx = M.l + slot * (i + 0.5);
      const color = seriesColor(i);
      const box = el('g', { stroke: color, 'stroke-width': 2, fill: 'none' });
      box.appendChild(el('line', { x1: cx, x2: cx, y1: sy(g.whisker_hi), y2: sy(g.q3) }));
      box.appendChild(el('line', { x1: cx, x2: cx, y1: sy(g.q1), y2: sy(g.whisker_lo) }));
      for (const v of [g.whisker_lo, g.whisker_hi]) {
        box.appendChild(el('line', { x1: cx - bw / 4, x2: cx + bw / 4, y1: sy(v), y2: sy(v) }));
      }
      box.appendChild(el('rect', { x: cx - bw / 2, y: sy(g.q3), width: bw,
                                   height: Math.max(1, sy(g.q1) - sy(g.q3)),
                                   fill: color, 'fill-opacity': 0.14 }));
      box.appendChild(el('line', { x1: cx - bw / 2, x2: cx + bw / 2, y1: sy(g.median), y2: sy(g.median),
                                   'stroke-width': 3 }));
      for (const o of g.outliers) {
        box.appendChild(el('circle', { cx, cy: sy(o.y), r: 4, fill: 'var(--paper)' }));
        marks.push({ kind: 'outlier', g, o, px: cx, py: sy(o.y), color });
      }
      struct.appendChild(box);
      marks.push({ kind: 'box', g, px: cx, x0: cx - slot / 2, x1: cx + slot / 2, color,
                   py: sy(g.median) });
    });
    const xCats = groups.map((g, i) => [M.l + slot * (i + 0.5), short(g.name)]);
    drawAxes(struct, { M, iw, ih, H, gy, sy, xCats, slant,
                       xLabel: slant ? null : spec.x_label, yLabel: spec.y_label });
    scale = { M, iw, ih };
  }

  let hover = null;
  function drawOverlay() {
    while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
    if (!hover) return;
    const { g } = hover;
    const lines = hover.kind === 'outlier'
      ? [hover.o.name, `${g.name} 的离群点`, `${spec.y_label}：${fmtValue(hover.o.y)}`,
         ...(onPick ? ['点一下打开这个样品'] : [])]
      : [g.name, `${g.n} 个样品`, `中位数 ${fmtValue(g.median)}`,
         `Q1–Q3 ${fmtValue(g.q1)} – ${fmtValue(g.q3)}`, `须 ${fmtValue(g.whisker_lo)} – ${fmtValue(g.whisker_hi)}`];
    tipBox(overlay, { x: hover.px, y: hover.py, color: hover.color, lines, ...scale });
  }
  const pick = (evt) => {
    const r = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const px = ((evt.clientX - r.left) / r.width) * vb.width;
    const py = ((evt.clientY - r.top) / r.height) * vb.height;
    const near = marks.find((m) => m.kind === 'outlier' && (m.px - px) ** 2 + (m.py - py) ** 2 < HIT * HIT);
    return near || marks.find((m) => m.kind === 'box' && px >= m.x0 && px < m.x1) || null;
  };
  svg.addEventListener('mousemove', (e) => {
    const next = pick(e);
    if (next !== hover) { hover = next; drawOverlay(); }
    svg.style.cursor = hover?.kind === 'outlier' && onPick ? 'pointer' : 'default';
  });
  svg.addEventListener('mouseleave', () => { hover = null; drawOverlay(); });
  if (onPick) {
    svg.addEventListener('click', (e) => { const m = pick(e); if (m?.kind === 'outlier') onPick(m.o); });
  }

  drawStructure();
  trackWidth(host, (w) => { W = w; drawStructure(); drawOverlay(); });
  host.toSVG = () => new XMLSerializer().serializeToString(svg);
  return host;
}
