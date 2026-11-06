// 虚拟滚动。上千行全塞进 DOM 会让浏览器卡死，只渲染视口里那几十行。
//
// 用固定行高换实现简单 —— 样品列表每行结构一样，本来就该等高。

import { h, clear } from '../ui.js';

/**
 * virtualList({ height, rowHeight, count, renderRow, overscan })
 * renderRow(index) → 元素
 * 返回的就是那个滚动容器节点
 */
export function virtualList({ height = 480, rowHeight = 46, count = 0,
                              renderRow, overscan = 6 }) {
  const spacer = h('div.vlist-spacer');
  const canvas = h('div.vlist-canvas');
  const host = h('div.vlist', { style: { height: `${height}px` } }, spacer, canvas);

  let total = count;
  let lastRange = [-1, -1];

  function layout(force = false) {
    spacer.style.height = `${total * rowHeight}px`;
    const scroll = host.scrollTop;
    const first = Math.max(0, Math.floor(scroll / rowHeight) - overscan);
    const visible = Math.ceil(height / rowHeight) + overscan * 2;
    const last = Math.min(total, first + visible);
    if (!force && first === lastRange[0] && last === lastRange[1]) return;
    lastRange = [first, last];

    // 键盘在列表里走的时候，焦点在某一行上。下面整块重画会把那个节点换掉，焦点就掉回 <body> ——
    // 每往下走几行就要重新 Tab 进来。重画完把焦点放回同一个序号的那一行
    const active = document.activeElement;
    const keep = canvas.contains(active) ? active.closest('[data-index]')?.dataset.index : null;

    clear(canvas);
    canvas.style.transform = `translateY(${first * rowHeight}px)`;
    for (let i = first; i < last; i++) {
      const row = renderRow(i);
      if (row) {
        row.style.height = `${rowHeight}px`;
        canvas.appendChild(row);
      }
    }
    if (keep !== null && keep !== undefined) {
      canvas.querySelector(`[data-index="${keep}"]`)?.focus({ preventScroll: true });
    }
  }

  let ticking = false;
  host.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; layout(); });
  });

  layout(true);
  return host;
}
