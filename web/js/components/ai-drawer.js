// 右侧 AI 抽屉。
//
// 三个设计决定，都是为了「边看图边问」这一件事：
//
// 1. **撑开 .shell 的第三列，不盖住主区。** 盖住的话你每问一句就得关掉它才能
//    看图，那还不如开个新标签页。
// 2. **宽度可拖，记在 localStorage。** 340px 够看回答，700px 够看一张表。
//    这是个人偏好，每次重设是骚扰。
// 3. **模型写的动作永远先变成卡片。** 筛选式渲染成能改的 chip，处理渲染成
//    要点一下的确认卡。错了你看到的是错的 chip，不是错的结果。

import { api } from '../api.js';
import { infoDot } from './info.js';
import { h, mount, $, toast, fmtInt, fmtTime, confirmDialog, modal, richText, inlineText } from '../ui.js';
import { getScope, onScope, scopeFilter, hasSelection } from '../scope.js';
import { modelSelect, onModelPicked, streamReply, syncSelect } from './chat-client.js';

const WIDTH_KEY = 'hte.ai.width';
const MIN_W = 320;
const MAX_W = 720;
const DEFAULT_W = 380;

const D = {
  open: false,
  full: false,
  view: 'chat',        // chat | history
  conv: null,          // 当前会话 id
  title: '新对话',
  messages: [],
  convs: [],
  scopeMode: 'all',    // selected | all —— 只有这两档，不是 plan/auto
  scopeTouched: false, // 你自己点过选择器没有。点过就不再自动跳档
  // AI 卡片「用这个范围继续问」收窄出来的范围：{filter, count}。
  // 有它的时候，发消息带的是它，而不是数据处理页的筛选 —— 原来 applyScope 只改了
  // 会话上存的范围，下一问发出时又被数据处理页的筛选盖掉，收窄从来没生效（自查 §6 #7）。
  // 你点「选中的 / 全部命中的」或开新对话时清掉。
  narrowed: null,
  scopeN: 0,
  allN: null,      // 「全部命中的」有几个，问后端要
  detailMax: 40,
  streaming: false,
  abort: null,
  model: null,         // {provider, model}；null = 用设置里的默认
  modelReady: null,    // null = 还没探过
  creating: null,      // 正在库里建会话的那个请求（第一次发送时才建）
};

let refs = {};
let ctx = { nav: () => {}, currentPage: () => '' };

// ------------------------------------------------------------------ 初始化
export function initDrawer(options = {}) {
  ctx = { ...ctx, ...options };
  refs.root = $('#aiDrawer');
  if (!refs.root) return;

  buildShell();
  // 关着的时候第三列必须是 0：--ai-w 不清零的话，抽屉明明没开，
  // 主区却一直被切掉 380px —— 样品页那两张并排的图会一起缩水。
  setWidth(0, { persist: false });

  $('#aiToggle')?.addEventListener('click', () => toggle());

  onModelPicked((v) => {
    const changed = !D.model || !v || D.model.model !== v.model || D.model.provider !== v.provider;
    D.model = v;
    syncSelect(refs.modelSel, v);
    if (changed && v && D.messages.length) {
      D.messages.push({ role: 'system', content: `已切换到 ${v.model}`, meta: {} });
      if (D.view === 'chat' && !D.streaming) paint();
    }
  });

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      toggle();
    } else if (e.key === 'Escape') {
      if (D.full) setFull(false);           // 先退全屏，别一下全关了
      // 弹窗开着时 Esc 是关弹窗的。原来查的是 .modal-mask —— 这个类名根本不存在，
      // 于是在弹窗里按 Esc 会连抽屉一起关掉
      else if (D.open && !$('.modal-backdrop')) toggle(false);
    }
  });

  // 数据处理页改了勾选或筛选式，范围选择器上的数字要跟着走
  onScope(() => { if (D.open) refreshScope(); });
}

function toggle(force) {
  D.open = force === undefined ? !D.open : force;
  refs.root.hidden = !D.open;
  document.documentElement.classList.toggle('ai-open', D.open);
  setWidth(D.open ? readWidth() : 0, { persist: false });
  $('#aiToggle')?.setAttribute('aria-expanded', String(D.open));
  if (D.open) {
    if (D.modelReady === null) probeModel();
    // 每次打开都重新拉一遍模型列表：抽屉是在启动时建的，那会儿多半还没配模型。
    // 只在建的时候拉一次的话，你去设置页配好回来，下拉还是空的。
    drawModelSwitch();
    refreshScope();
    // 打开抽屉不建会话 —— 原来每打开一次就在库里建一个，历史里攒一串「新对话 · 0 条」。
    // 第一次发送时才建（ensureConversation）
    if (!D.conv && !D.messages.length) paint();
    refs.input?.focus();
  }
}

// ------------------------------------------------------------------ 骨架
function buildShell() {
  const grip = h('div.ai-grip', { title: '拖动改变宽度' });
  bindDrag(grip);

  refs.title = h('div.ai-title', D.title);
  refs.body = h('div.ai-body');
  refs.input = h('textarea.ai-input', {
    rows: 2, placeholder: '问点什么…（Enter 发送，Shift+Enter 换行）',
    oninput: autoGrow,
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    },
  });
  refs.scopeBar = h('div.ai-scope');
  refs.modelBar = h('span');
  drawModelSwitch();
  // 一个 handler 按状态分发。h() 用的是 addEventListener，后面再赋 .onclick
  // 只会**再加**一个监听器而不是替换掉 —— 那样点「停止」会既停又重发一遍。
  refs.sendBtn = h('button.btn.btn-primary.btn-sm',
                   { onclick: () => (D.streaming ? stop() : send()) }, '发送');

  mount(refs.root,
    grip,
    h('div.ai-head',
      h('button.icon-btn', { title: '菜单', onclick: (e) => openMenu(e) }, '⋯'),
      refs.title,
      refs.modelBar,
      h('button.icon-btn', { title: '全屏', onclick: () => setFull(!D.full) }, '⤢'),
      h('button.icon-btn', { title: '关闭', onclick: () => toggle(false) }, '✕')),
    refs.body,
    h('div.ai-foot', refs.scopeBar, h('div.ai-compose', refs.input, refs.sendBtn)));
}

/**
 * 头部那个模型切换器。选择本身、下拉、存在哪儿，都在 chat-client.js ——
 * 和 ⓘ 浮层共用一份：在哪边换，另一边立刻跟上（见 initDrawer 里的 onModelPicked）。
 */
async function drawModelSwitch() {
  if (!refs.modelBar) return;
  const { el, current } = await modelSelect({ title: '这次对话用哪个模型' });
  D.model = current;
  refs.modelSel = el;
  mount(refs.modelBar, el);
}

function autoGrow() {
  const el = refs.input;
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
}

// ------------------------------------------------------------------ 宽度
function readWidth() {
  try {
    const v = Number(localStorage.getItem(WIDTH_KEY));
    if (v >= MIN_W && v <= MAX_W) return v;
  } catch { /* 隐私模式下 localStorage 会抛 */ }
  return DEFAULT_W;
}

function setWidth(px, { persist = true } = {}) {
  // 全屏时不占 grid 的位置，否则主区会被挤成一条缝
  const w = D.full ? 0 : Math.round(px);
  document.documentElement.style.setProperty('--ai-w', `${w}px`);
  if (persist && px >= MIN_W) {
    try { localStorage.setItem(WIDTH_KEY, String(Math.round(px))); } catch { /* 同上 */ }
  }
}

function bindDrag(grip) {
  let startX = 0;
  let startW = 0;

  grip.addEventListener('pointerdown', (e) => {
    if (D.full) return;
    startX = e.clientX;
    startW = refs.root.getBoundingClientRect().width;
    grip.setPointerCapture(e.pointerId);   // 拖到窗口外面也不掉
    document.body.classList.add('is-resizing');
    e.preventDefault();
  });

  grip.addEventListener('pointermove', (e) => {
    if (!grip.hasPointerCapture?.(e.pointerId)) return;
    // 抽屉在右边，往左拖是变宽，所以是 startX - clientX
    const w = Math.min(MAX_W, Math.max(MIN_W, startW + (startX - e.clientX)));
    setWidth(w);
  });

  const end = (e) => {
    if (grip.hasPointerCapture?.(e.pointerId)) grip.releasePointerCapture(e.pointerId);
    document.body.classList.remove('is-resizing');
  };
  grip.addEventListener('pointerup', end);
  grip.addEventListener('pointercancel', end);
}

function setFull(on) {
  D.full = on;
  refs.root.classList.toggle('is-full', on);
  setWidth(on ? 0 : readWidth(), { persist: false });
}

// ------------------------------------------------------------------ 菜单
function openMenu(evt) {
  // 还没发过一句话时，这个对话在库里还不存在：重命名 / 导出 / 删除都无从谈起，不摆出来
  const saved = !!D.conv;
  const items = [
    ['新对话', () => startConversation()],
    ['历史对话', () => showHistory()],
    saved && ['重命名', () => renameCurrent()],
    saved && ['导出为 Markdown', () => exportMarkdown()],
    [D.full ? '退出全屏' : '全屏', () => setFull(!D.full)],
    saved && ['删除这个对话', () => deleteCurrent(), 'danger'],
  ].filter(Boolean);
  const menu = h('div.ai-menu',
    ...items.map(([label, fn, kind]) =>
      h(`button.ai-menu-item${kind === 'danger' ? '.is-danger' : ''}`, {
        onclick: () => { close(); fn(); },
      }, label)));

  const rect = evt.currentTarget.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.left = `${rect.left}px`;
  document.body.appendChild(menu);

  const close = () => { menu.remove(); document.removeEventListener('pointerdown', away, true); };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener('pointerdown', away, true), 0);
}

// ------------------------------------------------------------------ 会话
/**
 * 开一个新对话 —— 只是把屏幕清空。库里的会话等第一次发送时才建（ensureConversation）。
 *
 * 原来这里立刻 POST 一个会话：每打开一次抽屉、每点一次「新对话」就在库里多一个，
 * 一句话没说的也算，历史里攒一串「新对话 · 0 条」（P12 浏览器验收时发现）。
 */
function startConversation() {
  stopStreamingForSwitch();
  D.conv = null;
  D.creating = null;
  D.narrowed = null;
  D.scopeTouched = false;      // 新对话回到「跟着你的选择走」的默认
  D.messages = [];
  D.title = '新对话';
  D.view = 'chat';
  refs.title.textContent = D.title;
  paint();
}

/**
 * 发送前确保库里有这个会话。连按两下回车只建一个：建的过程中再来的复用同一个请求。
 * 建好之前你又点了「新对话」或打开了历史（D.creating 被换掉），这个结果就不认了。
 */
function ensureConversation() {
  if (D.conv) return Promise.resolve(D.conv);
  if (D.creating) return D.creating;
  const mine = D.creating = api.newConversation(scopeFilter(D.scopeMode))
    .then((r) => {
      if (D.creating === mine) D.conv = r.conversation.conversation_id;
      return D.conv;
    })
    .catch((err) => { showError(err); return null; })
    .finally(() => { if (D.creating === mine) D.creating = null; });
  return mine;
}

async function openConversation(id) {
  stopStreamingForSwitch();
  D.creating = null;
  try {
    const r = await api.conversation(id);
    D.conv = id;
    D.narrowed = null;
    D.title = r.conversation.title;
    D.messages = r.messages;
    D.view = 'chat';
    refs.title.textContent = D.title;
    paint();
  } catch (err) {
    showError(err);
  }
}

/**
 * 换对话（新开、打开历史、看历史列表）之前，停掉正在流的那个回答。
 *
 * 原来不停：旧回答的字继续往屏幕最后一个气泡里写 —— 而那已经是另一个对话、
 * 甚至是历史列表的某一行；D.streaming 还一直是 true，新对话里发不出消息（自查 §6 #23）。
 */
function stopStreamingForSwitch() {
  if (D.streaming) D.abort?.abort();
}

async function showHistory() {
  stopStreamingForSwitch();
  D.view = 'history';
  paint();
  try {
    D.convs = (await api.conversations()).conversations || [];
  } catch (err) {
    D.convs = [];
    showError(err);
  }
  if (D.view === 'history') paint();
}

async function renameCurrent() {
  if (!D.conv) return;
  const input = h('input.input', { value: D.title, style: 'width:100%' });
  const m = modal({
    title: '重命名对话',
    body: input,
    foot: h('button.btn.btn-primary', {
      onclick: async () => {
        const t = input.value.trim();
        if (t) {
          await api.patchConversation(D.conv, { title: t });
          D.title = t;
          refs.title.textContent = t;
        }
        m.close();
      },
    }, '保存'),
  });
  input.focus();
}

async function deleteCurrent() {
  if (!D.conv) return;
  if (!await confirmDialog(`删掉「${D.title}」？钉在对比上的分析会留下。`,
                           { confirmLabel: '删除', danger: true })) return;
  await api.deleteConversation(D.conv);
  toast('对话已删除');
  startConversation();
}

function exportMarkdown() {
  const lines = [`# ${D.title}`, ''];
  for (const m of D.messages) {
    lines.push(m.role === 'user' ? '## 我' : '## 助手', '', m.content || '（空）', '');
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const a = h('a', { href: URL.createObjectURL(blob), download: `${D.title || '对话'}.md` });
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ------------------------------------------------------------------ 数据范围
async function refreshScope() {
  // 默认档跟着你的选择走：勾了东西就默认分析勾中的，没勾就默认分析全部命中的。
  // 你**自己点过**选择器之后就不再自动跳了 —— 手动选择优先于默认值，
  // 不然每次勾选变化都把你刚选的那一档弹回去。
  if (!D.scopeTouched) D.scopeMode = hasSelection() ? 'selected' : 'all';
  else if (!hasSelection() && D.scopeMode === 'selected') D.scopeMode = 'all';
  drawScopeBar();

  // 只问「全部」那一档 —— 「选中的」有几个本地就知道，不值得跑一趟后端。
  // 而且这个数不能靠 process 页发布的 total：你可能压根没去过那一页。
  try {
    const r = await api.scopePreview(scopeFilter('all'));
    D.allN = r.n_samples;
    D.detailMax = r.detail_max;
  } catch {
    D.allN = null;
  }
  D.scopeN = D.narrowed ? (D.narrowed.count ?? 0)
    : D.scopeMode === 'selected' ? getScope().checked.length : (D.allN ?? 0);
  D.needsNarrowing = D.scopeN > D.detailMax;
  drawScopeBar();
}

/** 这一问带什么范围：收窄过就带收窄的，否则跟着数据处理页的选择。 */
function currentScope() {
  return D.narrowed ? { mode: 'all', filter: D.narrowed.filter } : scopeFilter(D.scopeMode);
}

/** 范围选择器。只有两档：分析你勾中的，还是分析筛出来的全部。 */
function drawScopeBar() {
  const nSel = getScope().checked.length;
  const nAll = D.allN;

  const seg = (mode, label, n, disabled) => h(
    `button.ai-seg${D.scopeMode === mode ? '.is-on' : ''}${disabled ? '.is-off' : ''}`,
    {
      disabled,
      title: disabled ? '还没有勾选任何样品' : '',
      onclick: () => { D.scopeMode = mode; D.scopeTouched = true; D.narrowed = null; refreshScope(); },
    },
    label, h('span.ai-seg-n', n == null ? '—' : fmtInt(n)));

  // 收窄生效时，两档都不亮，单独一枚显示「AI 收窄的 N 个」，点 × 撤销
  const narrowedChip = D.narrowed
    ? h('span.ai-seg.is-on', { title: '这一段对话只看 AI 卡片收窄出来的样品。点 × 回到你的选择' },
        'AI 收窄的', h('span.ai-seg-n', fmtInt(D.narrowed.count ?? 0)),
        h('button.icon-btn', { 'aria-label': '撤销收窄', style: { marginLeft: '4px' },
          onclick: () => { D.narrowed = null; refreshScope(); } }, '✕'))
    : null;

  mount(refs.scopeBar,
    h('div.ai-seg-row',
      D.narrowed ? narrowedChip : seg('selected', '选中的', nSel, nSel === 0),
      D.narrowed ? null : seg('all', '全部命中的', nAll),
      infoDot('ai_scope')),
    D.needsNarrowing
      ? h('div.ai-scope-note',
          `${fmtInt(D.scopeN)} 个样品超过逐条阅读上限（${D.detailMax}），`,
          '这一问只会拿到汇总统计，模型应该先提议一个收窄的范围。')
      : null);
}

// ------------------------------------------------------------------ 发消息
async function send() {
  const text = refs.input.value.trim();
  if (!text || D.streaming) return;

  // 先清输入框再去建会话：建的那一下要等后端，这期间再按一次回车看到的是空框，不会发两遍
  refs.input.value = '';
  autoGrow();
  if (!D.conv && !await ensureConversation()) {
    refs.input.value = text;             // 没建成（出错已经提示了）：话还给你
    autoGrow();
    return;
  }

  D.messages.push({ role: 'user', content: text, meta: {} });
  // 后端会拿第一句话给对话起名（chat.py 里那段），这里跟着改，
  // 不然标题一直挂着「新对话」，历史列表里却已经是问题原文了
  if (D.title === '新对话') {
    D.title = text.slice(0, 40);
    refs.title.textContent = D.title;
  }
  const assistant = { role: 'assistant', content: '', meta: {}, streaming: true };
  D.messages.push(assistant);
  const thread = D.messages;          // 这个回答属于哪一个对话 —— 换了对话就不再往屏幕上写
  const mine = () => D.messages === thread && D.view === 'chat';
  paint();

  D.streaming = true;
  D.abort = new AbortController();
  refs.sendBtn.textContent = '停止';
  refs.sendBtn.classList.remove('btn-primary');

  try {
    await streamReply(D.conv, {
      content: text, scope: currentScope(),
      ...(D.model || {}),          // 没选就不带，后端按设置里的默认走
    }, {
      signal: D.abort.signal,
      reply: assistant,
      onDelta: () => { if (mine()) paintLast(); },
    });
  } finally {
    D.streaming = false;
    D.abort = null;
    refs.sendBtn.textContent = '发送';
    refs.sendBtn.classList.add('btn-primary');
    if (mine()) paint();
  }
}

function stop() {
  D.abort?.abort();
}

// ------------------------------------------------------------------ 渲染
function paint() {
  if (D.view === 'history') return paintHistory();

  if (!D.messages.length) return paintEmpty();
  mount(refs.body, ...D.messages.map(bubble));
  scrollDown();
}

/** 流式时只重画最后一条 —— 每个字重建整个列表会把滚动和选中都打断。 */
function paintLast() {
  const last = refs.body.lastElementChild;
  const msg = D.messages[D.messages.length - 1];
  if (!last || !msg) return paint();
  last.replaceWith(bubble(msg));
  scrollDown();
}

function scrollDown() {
  refs.body.scrollTop = refs.body.scrollHeight;
}

function paintEmpty() {
  mount(refs.body,
    h('div.ai-empty',
      h('div.ai-empty-title', 'AI 助手'),
      h('p.small.dim', ...inlineText(
        '它能看到你当前范围里的**汇总统计、标量和每条曲线的摘要**'
        + '（批处理算出的曲线，按时间分成十几到三十段取平均），看不到原始光谱矩阵。')),
      h('div.ai-hints',
        ...['这批样品的膜厚分布怎么样？',
            '把 ZG0014 的两次测量筛出来',
            '为什么大半样品的条纹数不够？'].map((q) =>
          h('button.ai-hint', {
            onclick: () => { refs.input.value = q; autoGrow(); refs.input.focus(); },
          }, q))),
      D.modelReady === false
        ? h('div.ai-nomodel',
            h('div.small', '还没有配模型。'),
            h('button.btn.btn-sm', {
              onclick: () => { toggle(false); ctx.nav('settings'); },
            }, '去「设置 → 模型」粘贴配置'))
        : null));
}

function paintHistory() {
  mount(refs.body,
    h('div.ai-histhead',
      h('span.small.dim', `${fmtInt(D.convs.length)} 个对话`),
      h('button.btn.btn-sm', { onclick: () => startConversation() }, '新对话')),
    ...(D.convs.length
      ? D.convs.map((c) => h('button.ai-histrow', {
          onclick: () => openConversation(c.conversation_id),
        },
        h('div.ai-histrow-title', c.title),
        h('div.xsmall.dim',
          `${fmtInt(c.n_messages || 0)} 条 · ${fmtTime(c.updated_at)}`)))
      : [h('div.ai-empty', h('p.small.dim', '还没有对话。'))]));
}

function bubble(m) {
  const meta = m.meta || {};
  const kids = [];

  // 动作卡片已经把那段 json 讲成人话了，正文里的围栏就不用再显示一遍
  const body = meta.card ? stripActionJson(m.content) : m.content;
  if (body) kids.push(...richText(body));
  if (m.streaming && !m.content) kids.push(h('span.ai-cursor', '正在想…'));

  if (meta.noModel) {
    kids.push(h('div.ai-nomodel',
      h('div.small', '还没有配模型，所以这一问没有答案。'),
      h('button.btn.btn-sm', {
        onclick: () => { toggle(false); ctx.nav('settings'); },
      }, '去「设置 → 模型」粘贴配置')));
  } else if (meta.error) {
    kids.push(h('div.ai-err', meta.error));
  } else if (meta.aborted) {
    kids.push(h('div.xsmall.dim', '（已停止）'));
  }

  if (meta.card) kids.push(actionCard(meta.card));

  if (m.role === 'assistant' && m.content && !m.streaming) {
    kids.push(h('div.ai-acts',
      h('button.ai-act', { title: '钉到某次对比', onclick: () => pinAnswer(m) }, '📌 钉住'),
      h('button.ai-act', {
        onclick: () => { navigator.clipboard?.writeText(m.content); toast('已复制'); },
      }, '复制')));
  }

  return h(`div.ai-msg.is-${m.role}`, ...kids);
}

/** 去掉正文末尾那段动作 json 围栏。只去 json 的，别的代码块要留着。 */
function stripActionJson(text) {
  return String(text || '')
    .replace(/```(?:json)?\s*\{[\s\S]*?"action"[\s\S]*?\}\s*```/g, '')
    .trim();
}

// ------------------------------------------------------------------ 动作卡片
const CARD_TITLE = {
  narrow: '模型建议先收窄范围',
  select: '模型给了一个筛选式',
  process: '模型建议跑一次批处理',
};

/** 筛选式 → 人能读的 chip。看不懂的键原样显示，不装作没有。 */
function describeChip(key, value) {
  const list = (v) => (Array.isArray(v) ? v : [v]).map((x) => String(x));
  switch (key) {
    case 'batch':   return list(value).map((v) => `样品号 ${v}`);
    case 'folder':  return list(value).map((v) => `文件夹 ${v}/`);
    case 'method':  return list(value).map((v) => `测量方法 ${v}`);
    // 波长/编号都不加千分位 —— 「1,120」读起来像个计数，不像一个波长
    case 'name_range': {
      const r = value || {};
      return [`名称 ${r.prefix || ''}${Number(r.min)}–${Number(r.max)}${r.suffix || ''}`];
    }
    case 'time': {
      const t = value || {};
      return [`时间 ${t.from || '最早'} → ${t.to || '最晚'}`];
    }
    case 'name_prefix': return [`名字以 ${value} 开头`];
    case 'q':           return [`搜索「${value}」`];
    case 'has_matrix':  return value ? ['有光谱矩阵'] : [];
    case 'ids':         return [`指定的 ${list(value).length} 个`];
    case 'exclude':     return [`排除 ${list(value).length} 个`];
    default:            return [`${key}: ${JSON.stringify(value)}`];
  }
}

function actionCard(card) {
  const flt = card.filter || {};
  const chips = Object.entries(flt).flatMap(([k, v]) => describeChip(k, v));

  const body = [
    h('div.ai-card-head', CARD_TITLE[card.action] || '建议'),
    card.why ? h('div.small.dim', card.why) : null,
    chips.length
      ? h('div.ai-chips', ...chips.map((c) => h('span.tag', c)))
      : h('div.xsmall.dim', '（没有给出筛选条件）'),
  ];

  if (card.filter_error) body.push(h('div.ai-err', card.filter_error));
  else if (card.count != null) {
    body.push(h('div.small', `会命中 ${fmtInt(card.count)} 个样品`));
  }

  if (card.action === 'process' && card.recipe) {
    body.push(h('div.xsmall.dim',
      `配方：${Object.entries(card.recipe)
        .map(([k, v]) => `${k}=${Number.isFinite(v) ? Number(v) : v}`).join(' · ')}`));
  }

  // 按钮的措辞要说清楚点下去会发生什么。「应用」比「确定」多一个字，
  // 但少一次「我点了会不会直接跑起来」的犹豫。
  const act = {
    narrow: ['用这个范围继续问', () => applyScope(card)],
    select: ['应用到数据处理页', () => applyToProcess(card)],
    process: ['去批处理对话框（不会自动开跑）', () => applyToProcess(card)],
  }[card.action];

  body.push(h('div.ai-card-foot',
    act ? h('button.btn.btn-sm.btn-primary', { onclick: act[1] }, act[0]) : null,
    h('button.btn.btn-sm.btn-ghost', {
      onclick: (e) => { e.currentTarget.closest('.ai-card')?.remove(); },
    }, '忽略')));

  return h('div.ai-card', ...body.filter(Boolean));
}

/** 让模型的筛选式先变成范围，下一问就在这个范围里。 */
async function applyScope(card) {
  if (!card.filter || !D.conv) return;
  try {
    await api.patchConversation(D.conv, { scope: { mode: 'all', filter: card.filter } });
  } catch (err) {
    toast(`没能收窄范围：${err.message}`, 'err');
    return;
  }
  D.narrowed = { filter: card.filter, count: card.count ?? null };
  D.scopeN = card.count ?? 0;
  D.needsNarrowing = D.scopeN > D.detailMax;
  drawScopeBar();
  D.messages.push({ role: 'system', content: `数据范围已收窄到 ${fmtInt(D.scopeN)} 个样品。`,
                    meta: {} });
  paint();
  toast(`范围已收窄到 ${fmtInt(D.scopeN)} 个`);
}

/** 把筛选式交给数据处理页。**只是填进去**，跑不跑你说了算。 */
function applyToProcess(card) {
  if (!card.filter) return;
  window.dispatchEvent(new CustomEvent('hte:apply-filter', {
    detail: { filter: card.filter, openBatch: card.action === 'process',
              recipe: card.recipe || null },
  }));
  ctx.nav('process');
  toast('筛选式已填进数据处理页');
}

// ------------------------------------------------------------------ 钉住
async function pinAnswer(m) {
  // 钉的是给人读的正文。那段动作 json 在抽屉里已经变成卡片了，
  // 钉到对比页上就只剩噪声 —— 何况那边没有能点的按钮。
  const note = (m.meta?.card ? stripActionJson(m.content) : m.content || '').trim();
  if (!note) { toast('这条没有正文可钉', 'warn'); return; }

  let runs = [];
  try {
    runs = (await api.batchRuns()).runs || [];
  } catch { /* 下面会显示「还没有对比」 */ }

  if (!runs.length) {
    toast('还没有跑过对比，没有地方可钉', 'warn');
    return;
  }

  const list = h('div.ai-pinlist', ...runs.slice(0, 20).map((r) => {
    let title = '未命名对比';
    try { title = JSON.parse(r.params_json || '{}').title || title; } catch { /* 用默认 */ }
    return h('button.ai-histrow', {
      onclick: async () => {
        try {
          await api.createPin({
            analysis_run_id: r.analysis_run_id,
            conversation_id: D.conv,
            message_id: m.meta?.message_id,
            note,
          });
          toast('已钉到这次对比');
          dlg.close();
        } catch (err) { showError(err); }
      },
    },
      h('div.ai-histrow-title', title),
      h('div.xsmall.dim',
        `${fmtInt(r.n_children)} 个样品 · ${fmtTime(r.started_at)}`));
  }));

  const dlg = modal({ title: '钉到哪一次对比？', body: list, width: 460 });
}

// ------------------------------------------------------------------ 杂项
async function probeModel() {
  try {
    const s = await api.assistStatus();
    D.modelReady = !!s.model_configured;
  } catch {
    D.modelReady = false;
  }
  if (D.view === 'chat' && !D.messages.length) paint();
}

function showError(err) {
  toast(err?.message || String(err), 'danger');
}
