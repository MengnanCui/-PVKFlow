// 虚拟滚动。上千行全塞进 DOM 会让浏览器卡死，只渲染视口里那几十行。
//
// 用固定行高换实现简单 —— 样品列表每行结构一样，本来就该等高。

import { h, clear } from '../ui.js';

/**
 * virtualList({ height, rowHeight, count, renderRow, overscan })
 * renderRow(index) → 元素
 * 返回的节点带 .refresh(newCount) 和 .scrollToIndex(i)
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

    clear(canvas);
    canvas.style.transform = `translateY(${first * rowHeight}px)`;
    for (let i = first; i < last; i++) {
      const row = renderRow(i);
      if (row) {
        row.style.height = `${rowHeight}px`;
        canvas.appendChild(row);
      }
    }
  }

  let ticking = false;
  host.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; layout(); });
  });

  host.refresh = (newCount) => {
    if (newCount !== undefined) total = newCount;
    lastRange = [-1, -1];
    layout(true);
  };
  host.scrollToIndex = (i) => { host.scrollTop = i * rowHeight; layout(true); };

  layout(true);
  return host;
}
