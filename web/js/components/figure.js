// 图与控件 —— 平台的版式基元。
//
// 这些原来住在 pages/sample.js 里。搬出来是因为**功能模块也要用它们**：
// 同事的模块只交声明，界面由平台按声明用这里的东西渲染出来。
// 所以「一格图长什么样」「控件长什么样」只有这一份定义，
// 平台自己的页面和同事的模块共用，风格想漂也漂不了。
//
// ⚠️ 这次是纯搬家，函数体一个字符都没改 —— 迁移的验收线是「行为完全不变」。

import { h, mount, fmtNum } from '../ui.js';

/**
 * 一格图。**结构固定成三行**：标题 / 功能模块 / 图。
 *
 * 这个约束是整个横排对齐的前提 —— `.fig-grid-*` 用 subgrid 把这三行的高度
 * 在**整行上**统一，取那一带里最高的。所以左右两格的图必然从同一条线开始，
 * 哪怕一边有俩滑块、另一边什么都没有（那一格就空着）。
 *
 * 说明文字走 `note`，放在图**下面** —— 放上面的话它会把同排的另一张图
 * 一起往下推，为了几行字浪费一整块竖向空间。
 */
export function figure(title, { head = null, ctl = null, body = null, note = null } = {}) {
  // 三行是**分开的三块**，不是「标题行里塞控件」：
  //   1 标题行  —— 只有标题和下载，所以每一格都一样高
  //   2 功能块  —— 所有控件都在这儿。没有控件就空着（subgrid 仍占一行）
  //   3 图
  //
  // 上一版把下拉、滑块塞进标题行的右侧，于是控件多的那一格标题行 100px、
  // 少的那一格 26px。图靠 subgrid 还是对齐的，但两个标题一高一低，
  // 看上去就是「没对齐」。控件全部下沉之后，标题行只剩一行文字，天然齐平。
  const ctls = [head, ctl].flat().filter(Boolean);
  return h('div.figure',
    h('div.figure-head', h('div.figure-title', title)),
    h('div.figure-ctl', ctls.length ? h('div.row.gap-3.wrap', ...ctls) : null),
    h('div.figure-body', body, note));
}

// ------------------------------------------------------------------ 控件
export function selectControl(label, value, options, onChange) {
  return h('label.inline-field',
    h('span.small.muted', label),
    h('select.select.select-sm', { onchange: (e) => onChange(e.target.value) },
      ...options.map(([v, t]) => h('option', { value: v, selected: v === value }, t))));
}

/**
 * 一个数字 + 一根滑块。
 *
 * `onCommit` 和 bandControl 里那对是同一个约定：拖动过程中连续触发 onChange
 * （前端算得起的事跟着走），松手/失焦才触发 onCommit（要打后端的等这一下）。
 * 不分开的话，拖一次滑块会发几十个请求，而且每个响应回来都重建一次界面 ——
 * 正在拖的那根滑块被换掉，拖动当场断掉。
 */
export function numberControl(label, value, min, max, step, onChange, onCommit = null) {
  // 边界取整。`<input type=range>` 的合法值是 **min + n×step**：光谱仪给的
  // lambda_min 是 330.276、step 是 1，于是 950 被吸附成 950.276 —— 数字框
  // 写 950、滑块停在 950.276，一碰就跳。和 bandControl 里是同一个坑。
  if (step >= 1) { min = Math.ceil(min); max = Math.floor(max); }
  const num = h('input.input.input-sm', {
    type: 'number', min, max, step, style: { width: '78px' },
  });
  // **先定边界再赋值。** h() 按对象顺序设属性，value 排在 max 前面的话，
  // 赋值那一刻 range 的 max 还是默认的 100 —— 950 当场被夹成 100，
  // 之后再把 max 改成 1120 也救不回来。症状就是数字框写 950、滑块停在最左边。
  // （这个顺序坑和上一轮 bandControl 那个「min+n×step 吸附」是一类：
  //   滑块和数字必须是同一个数，差一点都不行。）
  const range = h('input.range', { type: 'range', min, max, step });
  num.value = value;
  range.value = value;
  const push = (v) => {
    const clamped = Math.max(min, Math.min(max, Number(v)));
    num.value = clamped;
    range.value = clamped;
    onChange(clamped);
  };
  num.oninput = (e) => push(e.target.value);
  range.oninput = (e) => push(e.target.value);       // 拖动时连续触发 —— 这才叫实时
  if (onCommit) {
    // range 的 change 在**松手时**才触发，正是「这一下要打后端」的时机
    num.onchange = () => onCommit(Number(num.value));
    range.onchange = () => onCommit(Number(range.value));
  }
  return h('label.inline-field', h('span.small.muted', label), num, range);
}

/**
 * 波段选择：两个滑块 + 两个数字框。
 * onLive 在拖动过程中连续触发（前端算得起），onCommit 在松手时触发（要打后端）。
 */
export function bandControl(min, max, get, onLive, onCommit, opts = {}) {
  // 波长用整数步进就够；时间轴要小数，所以步长和最小跨度都可配。
  const step = opts.step ?? 1;
  const minSpan = opts.minSpan ?? 5;
  const round = (v) => (step >= 1 ? Math.round(v) : Number(v.toFixed(3)));

  // 边界取整。`<input type=range>` 的合法值是 **min + n×step** —— 光谱仪给的
  // lambda_min 是 330.276，step=1，于是 value=775 被浏览器吸附到 775.276：
  // 数字框写着 775，滑块停在 775.276，一碰就跳成 776.276。
  // 数字框和滑块必须用同一套边界，775 才是 775。
  const lo0Bound = step >= 1 ? Math.ceil(min) : min;
  const hi0Bound = step >= 1 ? Math.floor(max) : max;

  const [lo0, hi0] = get();
  const numAttrs = { type: 'number', min: lo0Bound, max: hi0Bound, step,
                     style: { width: '76px' } };
  const loNum = h('input.input.input-sm', { ...numAttrs, value: lo0 });
  const hiNum = h('input.input.input-sm', { ...numAttrs, value: hi0 });
  const rangeAttrs = { type: 'range', min: lo0Bound, max: hi0Bound, step };
  const loRange = h('input.range', { ...rangeAttrs, value: lo0 });
  const hiRange = h('input.range', { ...rangeAttrs, value: hi0 });

  const clamp = (lo, hi) => {
    lo = Math.max(lo0Bound, Math.min(hi0Bound - minSpan, lo));
    hi = Math.min(hi0Bound, Math.max(lo + minSpan, hi));
    return [round(lo), round(hi)];
  };

  /** 四个控件全部对齐到同一对值，并把结果送出去。 */
  const settle = (lo, hi, { skip = null } = {}) => {
    const [a, b] = clamp(lo, hi);
    // 正在打字的那个框不回写 —— 回写就是「敲一个字重排一次」，多位数永远输不完
    if (skip !== loNum) loNum.value = a;
    if (skip !== hiNum) hiNum.value = b;
    loRange.value = a;
    hiRange.value = b;
    onLive(a, b);
    return [a, b];
  };

  // ── 数字框：打字期间只解析，**绝不回写自己**。
  //
  // 上一版在 oninput 里直接 clamp 并回写：想输 800，敲下第一个 `8` 的瞬间
  // 就被钳成 330 —— 于是「波段无法打字只能滑动」。
  // 半途中的非法值（空串、只敲了一个 8、比另一头还大）不往下传，
  // 图保持上一次的样子，不闪也不报错；松开焦点时才归一。
  let committed = clamp(lo0, hi0);
  const typing = (el, other, isLo) => {
    el.oninput = () => {
      const v = Number(el.value);
      if (el.value === '' || !Number.isFinite(v)) return;
      const o = Number(other.value);
      if (!Number.isFinite(o)) return;
      const [lo, hi] = isLo ? [v, o] : [o, v];
      if (v < lo0Bound || v > hi0Bound || hi - lo < minSpan) return;  // 还没输完
      settle(lo, hi, { skip: el });
    };
    // 归一放在 change/blur：这时候才知道你输完了。
    // 每一头各自兜底到滑块上的当前值 —— 只清空了一个框时，另一头不该变成 NaN。
    const finish = () => {
      const v = Number(el.value);
      const o = Number(other.value);
      const mine = Number.isFinite(v) ? v : Number(isLo ? loRange.value : hiRange.value);
      const theirs = Number.isFinite(o) ? o : Number(isLo ? hiRange.value : loRange.value);
      const [a, b] = settle(...(isLo ? [mine, theirs] : [theirs, mine]));
      // change 和 blur 会接连触发。值没变就别再打一次后端 ——
      // 一次输入换来两个请求，图会闪两下。
      if (a !== committed[0] || b !== committed[1]) {
        committed = [a, b];
        onCommit?.();
      }
    };
    el.onchange = finish;
    el.onblur = finish;
  };
  typing(loNum, hiNum, true);
  typing(hiNum, loNum, false);

  loRange.oninput = () => settle(Number(loRange.value), Number(hiNum.value));
  hiRange.oninput = () => settle(Number(loNum.value), Number(hiRange.value));
  if (onCommit) {
    // 打后端的操作等松手，别在拖动过程中发几十个请求
    const slid = () => {
      committed = [Number(loRange.value), Number(hiRange.value)];
      onCommit();
    };
    loRange.onchange = slid;
    hiRange.onchange = slid;
  }

  // 初值也过一遍 clamp，保证一上来滑块和数字框就是同一个数
  const [a, b] = clamp(lo0, hi0);
  loNum.value = loRange.value = a;
  hiNum.value = hiRange.value = b;

  return h('div.band-control',
    h('div.row.gap-2', h('span.small.muted', opts.label ?? '波段'), loNum,
      h('span.small.dim', '–'), hiNum,
      h('span.small.dim', opts.unit ?? 'nm')),
    h('div.band-sliders', loRange, hiRange));
}
