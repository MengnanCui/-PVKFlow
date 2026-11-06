// 数据处理页：在列表上拖选。
//
// 按下一行开始，拖过的行跟着勾上（或取消，看起点那一行原来是什么状态）；
// 拖到列表上下边缘自动滚，四十个样品不用点四十下。

import { S, hooks, refs } from './process-state.js';

// ------------------------------------------------------------------ 勾选
//
// 挑样品是这一页的主要动作，所以**整行就是勾选热区**，点哪儿都是选。
// 打开样品要走行尾的「打开」按钮或者双击 —— 之前点行体直接跳走，
// 挑到一半被弹到详情页，回来滚动和勾选全没了。
//
// 三种手势：
//   点         切换这一行，并把锚点挪过来
//   按住拖     从按下那一行刷到当前行，按下时是选就一路选、是取消就一路取消
//   shift+点   锚点到这一行之间**加选**（已经选好的不会被抹掉）
//
// 复选框本身设了 pointer-events: none —— 它只是状态的显示，不是独立的按钮。
// 让它可点会有两套状态在打架：浏览器自己的 checked 和我们的 S.checked。

const drag = { on: false, anchor: null, mode: 'add' };

function selectRange(a, b, mode) {
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) {
    const r = S.rows[i];
    if (!r) continue;
    if (mode === 'add') S.checked.add(r.sample_id);
    else S.checked.delete(r.sample_id);
  }
}

export function beginSelect(index, evt) {
  const row = S.rows[index];
  if (!row) return;

  if (evt.shiftKey && S.anchor !== null && S.anchor !== index) {
    selectRange(S.anchor, index, 'add');
    hooks.syncSelection();
    return;                       // shift 是一次性的范围选，不进入拖拽
  }

  // 按下时这一行是「将要被选中」还是「将要被取消」，决定整段刷的方向。
  // 从一个已选的行开始拖 = 擦掉一片，这跟表格软件里的行为一致。
  drag.on = true;
  drag.anchor = index;
  pointer.x = evt.clientX;
  pointer.y = evt.clientY;
  autoScroll();
  drag.mode = S.checked.has(row.sample_id) ? 'remove' : 'add';
  S.anchor = index;
  selectRange(index, index, drag.mode);
  hooks.syncSelection();
}

export function extendSelect(index) {
  if (!drag.on) return;
  selectRange(drag.anchor, index, drag.mode);
  hooks.syncSelection();
}

// 松手可能发生在列表外面（拖出了窗口），所以监听在 window 上，只装一次
window.addEventListener('pointerup', endDrag);
window.addEventListener('pointercancel', endDrag);
window.addEventListener('pointermove', (e) => {
  if (!drag.on) return;
  pointer.x = e.clientX;
  pointer.y = e.clientY;
});

function endDrag() {
  drag.on = false;
  if (raf) { cancelAnimationFrame(raf); raf = null; }
}

// 拖到列表上下边缘就自动滚 —— 没有这个的话一次只能刷视口里那十几行，
// 四十个样品想全选还是得点点点。
const pointer = { x: 0, y: 0 };
const EDGE = 34;          // 离边缘多近开始滚
let raf = null;

function autoScroll() {
  raf = requestAnimationFrame(function step() {
    if (!drag.on) { raf = null; return; }
    const list = refs.vlist;
    if (list) {
      const r = list.getBoundingClientRect();
      let dy = 0;
      if (pointer.y < r.top + EDGE) dy = -Math.ceil((r.top + EDGE - pointer.y) / 2);
      else if (pointer.y > r.bottom - EDGE) dy = Math.ceil((pointer.y - (r.bottom - EDGE)) / 2);
      if (dy) list.scrollTop += dy;

      // 内容在不动的指针下面滚过去时 pointerenter 不会触发，
      // 所以每一帧都自己问一次「现在指针底下是哪一行」。
      const el = document.elementFromPoint(pointer.x, pointer.y);
      const row = el?.closest?.('.vrow[data-index]');
      if (row) extendSelect(Number(row.dataset.index));
    }
    raf = requestAnimationFrame(step);
  });
}
