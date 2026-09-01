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
    else if (k === 'html') node.innerHTML = v;
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
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

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
const toastStack = h('div.toast-stack');
document.addEventListener('DOMContentLoaded', () => document.body.appendChild(toastStack));

export function toast(message, kind = 'info', ms = 3600) {
  if (!toastStack.isConnected) document.body.appendChild(toastStack);
  const node = h(`div.toast.toast-${kind}`, h('div.grow', message));
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
export function modal({ title, body, foot, width, onClose, flush = false }) {
  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };

  const panel = h('div.modal', { style: width ? { width } : null },
    h('div.modal-head',
      h('h3', title),
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
  document.body.appendChild(backdrop);
  panel.querySelector('input,select,textarea,button')?.focus();
  return { close, panel, backdrop };
}

export function confirmDialog(message, { confirmLabel = '确定', danger = false } = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title: '请确认',
      body: h('p', message),
      foot: [
        h('button.btn', { onclick: () => { m.close(); resolve(false); } }, '取消'),
        h(`button.btn.${danger ? 'btn-danger' : 'btn-primary'}`,
          { onclick: () => { m.close(); resolve(true); } }, confirmLabel),
      ],
      onClose: () => resolve(false),
    });
  });
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

export function errorBox(err, retry) {
  const msg = err?.message || String(err);
  return h('div.notice.notice-danger',
    h('div.grow',
      h('div', msg),
      err?.detail ? h('pre', err.detail) : null,
      retry ? h('div.mt-2', h('button.btn.btn-sm', { onclick: retry }, '重试')) : null));
}

/** 异步渲染的统一包装：加载 → 成功 / 失败，三种状态都有交代。 */
export async function render(host, loader, view) {
  mount(host, skeletonRows(4));
  try {
    const data = await loader();
    // 骨架屏（四根灰条）换成整页内容，跳变很大 —— 让内容淡入进来，
    // 眼睛就有个「换了」的交代，不是凭空闪一下。
    const body = h('div.enter', view(data));
    mount(host, body);
    return data;
  } catch (err) {
    mount(host, errorBox(err, () => render(host, loader, view)));
    return null;
  }
}

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
