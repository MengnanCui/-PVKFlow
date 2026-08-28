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
  scopeN: 0,
  allN: null,      // 「全部命中的」有几个，问后端要
  detailMax: 40,
  streaming: false,
  abort: null,
  models: [],          // 设置里配好的全部模型，头部那个下拉从这儿来
  model: null,         // {provider, model}；null = 用设置里的默认
  modelReady: null,    // null = 还没探过
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

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      toggle();
    } else if (e.key === 'Escape') {
      if (D.full) setFull(false);           // 先退全屏，别一下全关了
      else if (D.open && !$('.modal-mask')) toggle(false);
    }
  });

  // 数据处理页改了勾选或筛选式，范围选择器上的数字要跟着走
  onScope(() => { if (D.open) refreshScope(); });
}

export function openDrawer() { toggle(true); }

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
    if (!D.conv) startConversation();
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
 * 头部那个模型切换器。
 *
 * 后端早就收 `{provider, model}` 了（chat.py 的 post_message），
 * 缺的只是一个能选的地方。选中的存 localStorage —— 这是「我这台机器上
 * 习惯用哪个」，不是平台设置，不该写进数据库让别人也跟着变。
 */
const MODEL_KEY = 'hte.ai.model';

function readModel() {
  try {
    const raw = localStorage.getItem(MODEL_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writeModel(v) {
  try {
    if (v) localStorage.setItem(MODEL_KEY, JSON.stringify(v));
    else localStorage.removeItem(MODEL_KEY);
  } catch { /* 隐私模式下写不进去，不影响这一次会话 */ }
}

async function drawModelSwitch() {
  if (!refs.modelBar) return;
  let list = [];
  try {
    const cfg = await api.models();
    for (const p of cfg.providers || []) {
      for (const m of p.models || []) list.push({ provider: p.name, model: m.id });
    }
  } catch { list = []; }

  D.models = list;
  // 只有一个模型时不画 —— 一个没有选择余地的下拉只是噪声
  if (list.length < 2) { mount(refs.modelBar); return; }

  const cur = readModel();
  const val = (x) => `${x.provider}\u0000${x.model}`;
  const sel = h('select.model-switch', {
    title: '这次对话用哪个模型',
    onchange: (e) => {
      const [provider, model] = e.target.value.split('\u0000');
      writeModel({ provider, model });
      D.model = { provider, model };
      // 在对话流里插一行 —— 回头看历史时才分得清哪句是谁答的
      if (D.messages.length) {
        D.messages.push({ role: 'system', content: `已切换到 ${model}`, meta: {} });
        paint();
      }
    },
  }, ...list.map((x) => h('option', {
    value: val(x), selected: cur && cur.model === x.model && cur.provider === x.provider,
  }, x.model)));

  D.model = cur && list.some((x) => x.model === cur.model && x.provider === cur.provider)
    ? cur : null;
  mount(refs.modelBar, sel);
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
  const items = [
    ['新对话', () => startConversation()],
    ['历史对话', () => showHistory()],
    ['重命名', () => renameCurrent()],
    ['导出为 Markdown', () => exportMarkdown()],
    [D.full ? '退出全屏' : '全屏', () => setFull(!D.full)],
    ['删除这个对话', () => deleteCurrent(), 'danger'],
  ];
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
async function startConversation() {
  D.conv = null;
  D.scopeTouched = false;      // 新对话回到「跟着你的选择走」的默认
  D.messages = [];
  D.title = '新对话';
  D.view = 'chat';
  refs.title.textContent = D.title;
  paint();
  try {
    const r = await api.newConversation(scopeFilter(D.scopeMode));
    D.conv = r.conversation.conversation_id;
  } catch (err) {
    showError(err);
  }
}

async function openConversation(id) {
  try {
    const r = await api.conversation(id);
    D.conv = id;
    D.title = r.conversation.title;
    D.messages = r.messages;
    D.view = 'chat';
    refs.title.textContent = D.title;
    paint();
  } catch (err) {
    showError(err);
  }
}

async function showHistory() {
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
  D.scopeN = D.scopeMode === 'selected' ? getScope().checked.length : (D.allN ?? 0);
  D.needsNarrowing = D.scopeN > D.detailMax;
  drawScopeBar();
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
      onclick: () => { D.scopeMode = mode; D.scopeTouched = true; refreshScope(); },
    },
    label, h('span.ai-seg-n', n == null ? '—' : fmtInt(n)));

  mount(refs.scopeBar,
    h('div.ai-seg-row',
      seg('selected', '选中的', nSel, nSel === 0),
      seg('all', '全部命中的', nAll),
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
  if (!D.conv) { await startConversation(); if (!D.conv) return; }

  refs.input.value = '';
  autoGrow();

  D.messages.push({ role: 'user', content: text, meta: {} });
  // 后端会拿第一句话给对话起名（chat.py 里那段），这里跟着改，
  // 不然标题一直挂着「新对话」，历史列表里却已经是问题原文了
  if (D.title === '新对话') {
    D.title = text.slice(0, 40);
    refs.title.textContent = D.title;
  }
  const assistant = { role: 'assistant', content: '', meta: {}, streaming: true };
  D.messages.push(assistant);
  paint();

  D.streaming = true;
  D.abort = new AbortController();
  refs.sendBtn.textContent = '停止';
  refs.sendBtn.classList.remove('btn-primary');

  try {
    await api.sendMessage(D.conv, {
      content: text, scope: scopeFilter(D.scopeMode),
      ...(D.model || {}),          // 没选就不带，后端按设置里的默认走
    }, {
      signal: D.abort.signal,
      onFrame: (event, data) => {
        if (event === 'meta') assistant.meta.message_id = data.message_id;
        else if (event === 'delta') { assistant.content += data.text; paintLast(); }
        else if (event === 'card') { assistant.meta.card = data; }
        else if (event === 'error') { assistant.meta.error = data.message; }
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') assistant.meta.aborted = true;
    else if (err.kind === 'no_model' || err.status === 501) assistant.meta.noModel = true;
    else assistant.meta.error = err.message;
  } finally {
    assistant.streaming = false;
    D.streaming = false;
    D.abort = null;
    refs.sendBtn.textContent = '发送';
    refs.sendBtn.classList.add('btn-primary');
    paint();
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
        '它能看到你当前范围里的**汇总统计和标量**，看不到原始光谱矩阵。')),
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
  D.scopeMode = 'all';
  await api.patchConversation(D.conv, { scope: { mode: 'all', filter: card.filter } });
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
