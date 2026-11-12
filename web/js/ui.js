// 极小的 DOM 工具集。没有框架、没有构建步骤——这是「打开就能用」的前提。

/**
 * h('div.klass#id', {attrs}, ...children)
 * 标签支持 tag、#id、.class 的任意组合与顺序：
 * 'section.page#page-overview' 和 'section#page-overview.page' 等价。
 */
export function h(tag, props = null, ...children) {
  const node = document.createElement(parseTagName(tag) || 'div');
  for (const token of tag.match(/[#.][\w-]+/g) || []) {
    if (token[0] === '#') node.id = token.slice(1);
    else node.classList.add(token.slice(1));
  }

  if (props && (props.nodeType || Array.isArray(props) || typeof props !== 'object')) {
    children.unshift(props);
    props = null;
  }
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.classList.add(...String(v).split(/\s+/).filter(Boolean));
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k in node && k !== 'list' && typeof v !== 'object') node[k] = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  append(node, children);
  return node;
}

function parseTagName(tag) {
  const m = /^[a-z][a-z0-9-]*/i.exec(tag);
  return m ? m[0] : '';
}

export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
export function mount(node, ...children) { clear(node); return append(node, children); }

// ------------------------------------------------------------------ 格式化
export function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

export function fmtNum(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v !== 'number') return String(v);
  if (!Number.isFinite(v)) return String(v);
  if (digits !== undefined) return v.toFixed(digits);
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e6 || a < 1e-4) return v.toExponential(3).replace('e', '×10^');
  if (Number.isInteger(v)) return v.toLocaleString('en-US');
  return v.toFixed(a < 1 ? 4 : a < 100 ? 3 : 2);
}

export function fmtInt(v) {
  return (v ?? 0).toLocaleString('en-US');
}

// 样品的身份是 (名字, 批次)。S1 在每个批次里都有一个 —— 只显示名字的话，
// 24 个不同的样品在列表里长得一模一样，根本分不出是哪一个。
export function sampleLabel(name, batch) {
  if (!name) return h('span.dim', '—');
  if (!batch) return name;
  return h('span', h('span.dim', batch + '/'), name);
}

export function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(+d)) return iso;
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

// ------------------------------------------------------------------ Toast
// 读屏软件靠 aria-live 才知道这里冒出了一条提示（自查 §6 #30）。出错的那种单独用 role=alert，
// 会打断着念
const toastStack = h('div.toast-stack', { role: 'status', 'aria-live': 'polite' });
document.addEventListener('DOMContentLoaded', () => document.body.appendChild(toastStack));

export function toast(message, kind = 'info', ms = 3600) {
  if (!toastStack.isConnected) document.body.appendChild(toastStack);
  const urgent = kind === 'err' || kind === 'danger';
  const node = h(`div.toast.toast-${kind}`, urgent ? { role: 'alert' } : null, h('div.grow', message));
  toastStack.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .18s, transform .18s';
    node.style.opacity = '0';
    node.style.transform = 'translateY(4px)';
    setTimeout(() => node.remove(), 200);
  }, ms);
  return node;
}

// ------------------------------------------------------------------ Modal
let modalSeq = 0;
// 开着的弹窗，后开的在最后。弹窗里再弹一个确认框时，Esc / Tab 只归最上面那个管 ——
// 两个都在 document 上听键盘，不分先后的话一下 Esc 会把两层一起关掉
const modalStack = [];

/**
 * 弹窗。无障碍（自查 §6 #30）：是一个 dialog、标题就是它的名字；
 * Tab 只在弹窗里转（不会跑到后面被遮住的页面上）；关掉之后焦点回到打开它的那个按钮。
 */
export function modal({ title, body, foot, width, onClose, flush = false }) {
  const opener = document.activeElement;
  const close = () => {
    const at = modalStack.indexOf(panel);
    if (at >= 0) modalStack.splice(at, 1);
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
    onClose?.();
    if (opener?.isConnected && typeof opener.focus === 'function') opener.focus();
  };
  const onKey = (e) => {
    if (modalStack[modalStack.length - 1] !== panel) return;
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key === 'Tab') trapTab(e, panel);
  };

  const titleId = `modal-title-${++modalSeq}`;
  const panel = h('div.modal', {
    role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
    style: width ? { width } : null,
  },
    h('div.modal-head',
      h(`h3#${titleId}`, title),
      // 右上角是「关掉这个窗」的通用手势，用 × 图标；
      // 底部那排才是这个窗自己的动作（取消 / 确定 / 关闭）。
      // 以前两处都是文字「关闭」，看起来像有两种不同的关闭方式。
      h('button.icon-btn', { onclick: close, title: '关闭 (Esc)',
                             'aria-label': '关闭' }, '✕')),
    h('div.modal-body' + (flush ? '.flush' : ''), body),
    foot ? h('div.modal-foot', foot) : null);

  const backdrop = h('div.modal-backdrop', {
    onclick: (e) => { if (e.target === backdrop) close(); },
  }, panel);

  document.addEventListener('keydown', onKey);
  modalStack.push(panel);
  document.body.appendChild(backdrop);
  panel.querySelector('input,select,textarea,button')?.focus();
  return { close, panel, backdrop };
}

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),'
  + 'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Tab / Shift+Tab 到了弹窗的头尾就绕回来；焦点不知怎么跑到外面去了，拉回第一个。 */
function trapTab(e, panel) {
  const items = [...panel.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (!panel.contains(active)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
}

/**
 * 问一句「确定吗」，确定 → true，取消 / Esc / 点外面 → false。
 *
 * 答案只在 onClose 里交出去一次。原来是「确定」按钮先 m.close() 再 resolve(true)，
 * 而 close() 会先跑 onClose → resolve(false)，Promise 只认第一次 —— 于是**点确定也返回 false**：
 * 装模块前的「我信得过，继续」、删 AI 对话、卸载模块，
 * 从界面上点都没有任何反应（D 组自查发现）。
 */
export function confirmDialog(message, { confirmLabel = '确定', danger = false } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const m = modal({
      title: '请确认',
      body: h('p', message),
      foot: [
        h('button.btn', { onclick: () => m.close() }, '取消'),
        h(`button.btn.${danger ? 'btn-danger' : 'btn-primary'}`,
          { onclick: () => { answer = true; m.close(); } }, confirmLabel),
      ],
      onClose: () => resolve(answer),
    });
  });
}

// ------------------------------------------------------------------ 共用小组件
/**
 * 指标卡：标签 + 大数字 + 一行说明。数字是 0 / 空时变淡（「还没有」和「有」一眼分得开）。
 * 原来总览、存储、批处理结果、QFLS 各抄了一份（技术债 C076）。
 * @param fmt 数字怎么写，默认整数千分位；QFLS 里有「58 / 60」这种现成的文字就传 String
 */
export function metric(label, value, note, fmt = fmtInt) {
  return h('div.metric',
    h('div.metric-label', label),
    h('div.metric-value' + (value ? '' : '.is-zero'), fmt(value)),
    note ? h('div.metric-note', note) : null);
}

/** 一排指标卡。items：[[标签, 数字, 说明?, 格式化?], …] */
export function metricRow(items, cols = 4) {
  return h(`div.metrics.metrics-${cols}`, ...items.map(([label, value, note, fmt]) => metric(label, value, note, fmt)));
}

/**
 * 分段控件（几个互斥的选项排成一条）。键盘：Tab 进来落在选中的那个上，左右 / 上下箭头换、
 * Home / End 到头尾 ——
 * 和单选按钮组的读屏约定一致（role=radiogroup）。原来智能体页一份、QFLS 抄了一份 CSS，都不认方向键。
 *
 * @param options [[值, 文字, 悬停说明?], …]（值按字符串比较）
 * @returns 元素，带 `.set(值)`：外面改了状态时同步高亮，不重画
 */
export function segmented(name, value, options, onPick) {
  const buttons = options.map(([v, text, title]) => h('button', {
    type: 'button', role: 'radio', title, dataset: { v: String(v) },
    onclick: () => { set(String(v)); onPick(v); },
  }, text));
  const el = h('span.seg', { role: 'radiogroup', 'aria-label': name }, ...buttons);
  function set(v) {
    for (const b of buttons) {
      const on = b.dataset.v === String(v);
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    }
  }
  el.addEventListener('keydown', (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (i < 0) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    const j = step ? (i + step + buttons.length) % buttons.length
      : e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1 : -1;
    if (j < 0) return;
    e.preventDefault();
    buttons[j].focus();
    buttons[j].click();
  });
  set(value);
  el.set = set;
  return el;
}

// ------------------------------------------------------------------ 状态视图
export function empty(text, action) {
  return h('div.empty', h('div.empty-text', text), action ? h('div.empty-action', action) : null);
}

/**
 * 加载占位。
 *
 * 形状要像它将要变成的东西 —— 以前是三根随机宽度的细灰条，
 * 换成整页内容时跳变很大，像闪了一下。现在给一个标题条 + 几个内容块，
 * 起码「这里要出现一段有标题的内容」这件事是对的。
 */
export function skeletonRows(n = 5) {
  return h('div.skeleton-stack',
    h('div.skeleton.skeleton-title'),
    ...Array.from({ length: Math.max(1, n - 1) }, (_, i) =>
      h('div.skeleton.skeleton-block', { style: { width: `${100 - (i % 3) * 12}%` } })));
}

/**
 * @param label 按钮上写什么。默认「重试」—— 但**只有真的会重来一次时才叫重试**。
 *   有几处传进来的回调其实是「回到上一页」，还写着重试就是骗人：
 *   点下去发现跑到别的页面去了。
 */
export function errorBox(err, retry, label = '重试') {
  const msg = err?.message || String(err);
  return h('div.notice.notice-danger',
    h('div.grow',
      h('div', msg),
      err?.detail ? h('pre', err.detail) : null,
      retry ? h('div.mt-2', h('button.btn.btn-sm', { onclick: retry }, label)) : null));
}

/**
 * 「只认最后一次」。
 *
 * 快速连续发请求时，先发的可能后到 —— 后到的旧响应会把新状态覆盖掉：
 * 快速点两下筛选，列表停在第一下的结果上；清空搜索框，旧的搜索结果又冒出来
 * （自查 §6 #18）。每个会被连续触发的地方各拿一个：
 *
 *   const ticket = latest();       // 模块级，一处一个
 *   const fresh = ticket();        // 发请求前取号
 *   const r = await api.xxx();
 *   if (!fresh()) return;          // 已经有更新的一次了，这次作废
 */
export function latest() {
  let n = 0;
  return () => { const my = ++n; return () => my === n; };
}

/** 异步渲染的统一包装：加载 → 成功 / 失败，三种状态都有交代。 */
export async function render(host, loader, view) {
  // 同一个容器上，后发的 render 说了算（见 latest()）
  host.__renderTicket ??= latest();
  const fresh = host.__renderTicket();
  mount(host, skeletonRows(4));
  try {
    const data = await loader();
    if (!fresh()) return null;
    // 骨架屏（四根灰条）换成整页内容，跳变很大 —— 让内容淡入进来，
    // 眼睛就有个「换了」的交代，不是凭空闪一下。
    const body = h('div.enter', view(data));
    mount(host, body);
    return data;
  } catch (err) {
    // 离开页面时请求被中止（api.with）：这不是错，页面已经不在了
    if (!fresh() || err?.name === 'AbortError') return null;
    mount(host, errorBox(err, () => render(host, loader, view)));
    return null;
  }
}

// ------------------------------------------------------------------ 状态
// 任务 / 运行 / 处理的状态，全平台一份叫法（原来五个页面各写一份：失败 / 没跑完、
// 进行中 / 运行中 / 处理中、
// 已取消 / 已停止，C070）。后端的状态值：queued | running | ok | failed | cancelled
export const STATUS = { queued: '排队中', running: '进行中', ok: '完成', failed: '失败', cancelled: '已停止' };
export const statusText = (s) => STATUS[s] || s || '';
export const statusClass = (s) =>
  s === 'ok' ? 'status-ok' : s === 'failed' ? 'status-danger'
    : s === 'running' || s === 'queued' ? 'status-accent' : 'status-warn';

export function busy(button, on) {
  button.classList.toggle('is-busy', on);
  button.disabled = on;
}

// ------------------------------------------------------------------ 轻量文本
/** 极简 markdown：围栏代码块 + 段落 + 粗体 + 行内代码。
 *
 * 不值得为它引一个库，但也不能不管 —— 模型是真的会吐 `**` 和反引号的，
 * 术语表的定义里也用粗体标了重点。原样显示出来就是一串星号，看着像坏了。
 *
 * 全程只造文本节点，不碰 innerHTML：模型的输出是不可信文本。
 */
export function richText(text, cls = 'ai-p') {
  const out = [];
  String(text ?? '').split(/```/).forEach((part, i) => {
    if (i % 2 === 1) { out.push(h('pre.ai-code', part.replace(/^\w*\n/, ''))); return; }
    for (const para of part.split(/\n{2,}/)) {
      const t = para.trim();
      if (t) out.push(h(`p.${cls}`, ...inlineText(t)));
    }
  });
  return out;
}

/** `**粗体**` 和 `` `行内代码` `` 切成节点。切不出来就原样当纯文本。 */
export function inlineText(text) {
  const out = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] !== undefined ? h('strong', m[1]) : h('code.ai-inline', m[2]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : [text];
}
