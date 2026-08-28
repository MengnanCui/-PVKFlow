// 界面上那些小小的 ⓘ。点开看这个词到底是什么意思，看不明白可以接着问模型。
//
// 为什么要有这东西：平台上到处是 DEGRADED、LOW_CYCLES、ot_quantum、
// 「分位数带」这种词。它们都有确切定义，但定义写在代码注释和规范文档里，
// 用户看不到 —— 于是要么猜，要么来问我。ⓘ 把定义搬到词的旁边。
//
// 三条设计决定：
//
// 1. **定义在前，对话在后。** 弹窗上半截永远是那段固定的权威定义，
//    下半截才是模型。模型可能说错话，那段定义不会 —— 用户永远有个
//    不依赖模型的答案兜底。
// 2. **每个术语一条独立的对话线。** 问「DEGRADED 是什么意思」不该和
//    上一次问光谱的对话混在一起。用 conversation.scope_json.topic 分线。
// 3. **历史存后端，不存 localStorage。** 清了浏览器缓存、换台机器，
//    问过的话都还在；而且在右侧 AI 抽屉的历史列表里也找得到同一条。
// 4. **是贴着按钮的小浮层，不是全屏弹窗。** 你查一个词的时候通常是想
//    「一边看图一边看定义」——全屏遮罩把图盖住了，等于逼你二选一。
//    所以没有遮罩：页面照常看得见、滚得动，点浮层外面就关。

import { h, mount, toast, fmtTime, richText } from '../ui.js';
import { api } from '../api.js';
import { term } from '../glossary.js';

/**
 * 造一个 ⓘ 按钮。
 *
 * @param {string} id  glossary.js 里的术语 id
 * @param {object} opts
 *   - `label`：只影响无障碍标签，不显示
 *   - `extra`：附加段，形如 `{ title, items: [{ key, label, note, count }] }`。
 *     给「这张图上真的出现过哪几档判级」这种**跟着当前数据走**的内容用 ——
 *     固定定义是所有页面共用的，这一段只属于这一张图。
 * @returns {HTMLElement|null}  术语不存在时返回 null —— 宁可不显示，
 *   也不要显示一个点开是空的 ⓘ（那比没有更糟：用户会以为是坏了）
 */
export function infoDot(id, opts = {}) {
  const t = term(id);
  if (!t) {
    // 开发期才会走到这儿。控制台留一句，别静默吞掉。
    console.warn('[info] 没有这个术语：', id);
    return null;
  }
  const btn = h('button.info-dot', {
    type: 'button',
    title: `${t.title} —— 点开看说明`,
    'aria-label': `${opts.label || t.title} 的说明`,
    'aria-expanded': 'false',
    onclick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      // 再点一次就收起来 —— 一个只能开不能关的按钮很讨厌
      if (btn.getAttribute('aria-expanded') === 'true') return closePop();
      openInfo(id, { anchor: btn, extra: opts.extra });
    },
  }, 'ⓘ');
  return btn;
}

/** 一行「词 + ⓘ」。给标题、图注、表头用，省得每处都写一遍 flex。 */
export function withInfo(text, id) {
  const dot = infoDot(id, { label: typeof text === 'string' ? text : undefined });
  return dot ? h('span.with-info', text, dot) : h('span', text);
}

// ------------------------------------------------------------------ 浮层
const TOPIC = (id) => `glossary:${id}`;

// 和右侧 AI 抽屉共用同一个键：在哪儿换的模型，另一边都跟着变。
// 各存一份的话，你在抽屉里换了模型、在 ⓘ 里问一句，答的还是旧模型。
const MODEL_KEY = 'hte.ai.model';

function readPickedModel() {
  try {
    const raw = localStorage.getItem(MODEL_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writePickedModel(v) {
  try { localStorage.setItem(MODEL_KEY, JSON.stringify(v)); } catch { /* 隐私模式 */ }
}

// 同时只开一个。开第二个之前先把上一个收掉 —— 满屏浮层比全屏遮罩还糟。
let openPop = null;

export function closePop() {
  openPop?.close();
}

/** 把浮层摆到锚点旁边，摆不下就翻到上面，左右夹在视口里不许出屏。 */
function place(pop, anchor) {
  const a = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  const hgt = pop.offsetHeight;
  const pad = 8;

  // 竖向：默认挂在下面；下面不够而上面够就翻上去
  const below = window.innerHeight - a.bottom;
  const flip = below < hgt + pad && a.top > below;
  let top = flip ? a.top - hgt - 6 : a.bottom + 6;
  top = Math.max(pad, Math.min(window.innerHeight - hgt - pad, top));

  // 横向：以锚点为中心，然后夹进视口
  let left = a.left + a.width / 2 - w / 2;
  left = Math.max(pad, Math.min(window.innerWidth - w - pad, left));

  pop.style.top = `${Math.round(top)}px`;
  pop.style.left = `${Math.round(left)}px`;
}

export function openInfo(id, { anchor = null, extra = null } = {}) {
  const t = term(id);
  if (!t) return;
  closePop();

  const S = { conv: null, messages: [], streaming: false, abort: null,
              modelReady: null, model: null };

  const thread = h('div.info-thread');
  const modelPick = h('span');
  const input = h('textarea.info-input', {
    rows: 1,
    placeholder: '还有哪里不明白？问一句…',
    oninput: () => grow(),
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    },
  });
  const sendBtn = h('button.btn.btn-sm.btn-primary', {
    onclick: () => (S.streaming ? stop() : send()),
  }, '问');

  function grow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  }

  const pop = h('div.info-pop', {
    role: 'dialog', 'aria-label': t.title,
    // 浮层里点一下不该被「点外面就关」逮到
    onpointerdown: (e) => e.stopPropagation(),
  },
    h('div.info-pop-head',
      h('div.info-pop-title', t.title),
      h('button.btn.btn-ghost.btn-xs', { onclick: () => close(), title: '关闭 (Esc)' }, '✕')),
    h('div.info-pop-body',
      // ── 上半截：固定定义。这一段不受模型影响。
      h('div.info-def',
        h('div.info-what', t.what),
        h('div.info-why', ...paragraphs(t.why))),
      extraBlock(extra),
      // ── 下半截：接着问
      h('div.info-chat',
        h('div.info-chat-head',
          h('span.dim', '接着问'),
          h('span.grow'),
          modelPick,
          h('button.btn.btn-ghost.btn-xs', {
            onclick: () => clearThread(),
            title: '清掉这个词下面问过的话',
          }, '清空')),
        thread,
        h('div.info-compose', input, sendBtn))));

  // 没有遮罩 —— 页面照常看得见、滚得动，这正是「不许遮挡整个页面」的意思
  document.body.appendChild(pop);
  anchor?.setAttribute('aria-expanded', 'true');
  if (anchor) place(pop, anchor);

  const reposition = () => { if (anchor) place(pop, anchor); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  const onAway = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };

  function close() {
    S.abort?.abort();
    pop.remove();
    anchor?.setAttribute('aria-expanded', 'false');
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onAway, true);
    // 页面滚动时跟着走。捕获阶段监听，内层滚动容器（.viewport）也能收到
    window.removeEventListener('scroll', reposition, true);
    window.removeEventListener('resize', reposition);
    if (openPop?.close === close) openPop = null;
  }
  openPop = { close };

  document.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', reposition, true);
  window.addEventListener('resize', reposition);
  // 延一帧再挂「点外面就关」：本次点击就是打开它的那一下，
  // 同一轮里挂上去会立刻把自己关掉
  requestAnimationFrame(() =>
    document.addEventListener('pointerdown', onAway, true));

  paint();
  loadHistory();
  probe();
  requestAnimationFrame(reposition);   // 内容渲染完高度才准

  // ---------------------------------------------------------------- 历史
  async function loadHistory() {
    try {
      const { conversations } = await api.conversations({ topic: TOPIC(id) });
      if (!conversations?.length) return;
      S.conv = conversations[0].conversation_id;
      const { messages } = await api.conversation(S.conv);
      S.messages = messages || [];
      paint();
    } catch {
      // 拉不到历史不该挡住看定义 —— 上半截已经渲染好了，这里就安静地算了
    }
  }

  async function clearThread() {
    if (!S.conv) { S.messages = []; return paint(); }
    try { await api.deleteConversation(S.conv); } catch { /* 已经没了也算成功 */ }
    S.conv = null;
    S.messages = [];
    paint();
  }

  async function probe() {
    try {
      const st = await api.assistStatus();
      S.modelReady = !!st.model_configured;
    } catch { S.modelReady = false; }
    if (!S.messages.length) paint();
    drawModelPick();
  }

  /** 这个小窗里也能换模型，和右侧抽屉共用同一份选择（存在同一个 localStorage 键）。 */
  async function drawModelPick() {
    let list = [];
    try {
      const cfg = await api.models();
      for (const pr of cfg.providers || []) {
        for (const m of pr.models || []) list.push({ provider: pr.name, model: m.id });
      }
    } catch { list = []; }
    // 只有一个模型时不画：一个没有选择余地的下拉只是噪声
    if (list.length < 2) return mount(modelPick);
    const cur = readPickedModel();
    const sep = '\u0000';
    mount(modelPick, h('select.model-switch', {
      title: '这一问用哪个模型',
      onchange: (e) => {
        const [provider, model] = e.target.value.split(sep);
        writePickedModel({ provider, model });
        S.model = { provider, model };
      },
    }, ...list.map((x) => h('option', {
      value: `${x.provider}${sep}${x.model}`,
      selected: cur && cur.model === x.model && cur.provider === x.provider,
    }, x.model))));
    S.model = cur && list.some((x) => x.model === cur.model && x.provider === cur.provider)
      ? cur : null;
  }

  // ---------------------------------------------------------------- 发问
  async function send() {
    const text = input.value.trim();
    if (!text || S.streaming) return;

    if (!S.conv) {
      try {
        const r = await api.newConversation({ topic: TOPIC(id), label: t.title }, t.title);
        S.conv = r.conversation.conversation_id;
      } catch (err) {
        toast(err?.message || '建不了对话', 'danger');
        return;
      }
    }

    input.value = '';
    grow();
    S.messages.push({ role: 'user', content: text, meta: {} });
    const a = { role: 'assistant', content: '', meta: {}, streaming: true };
    S.messages.push(a);
    paint();

    S.streaming = true;
    S.abort = new AbortController();
    sendBtn.textContent = '停止';

    try {
      await api.sendMessage(S.conv, {
        content: text,
        scope: { topic: TOPIC(id), label: t.title },
        ...(S.model || {}),        // 没选就不带，后端按设置里的默认走
        // 定义原文跟着走。术语表是界面文案，后端不该另存一份 ——
        // 两份定义各自维护迟早对不上，那时候用户看到的和模型说的就是两回事了。
        context_note: `${t.title}\n${t.what}\n${t.why}`,
      }, {
        signal: S.abort.signal,
        onFrame: (event, data) => {
          if (event === 'delta') { a.content += data.text; paintLast(); }
          else if (event === 'error') { a.meta.error = data.message; }
        },
      });
    } catch (err) {
      if (err.name === 'AbortError') a.meta.aborted = true;
      else if (err.kind === 'no_model' || err.status === 501) a.meta.noModel = true;
      else a.meta.error = err.message;
    } finally {
      a.streaming = false;
      S.streaming = false;
      S.abort = null;
      sendBtn.textContent = '问';
      paint();
    }
  }

  function stop() { S.abort?.abort(); }

  // ---------------------------------------------------------------- 渲染
  function paint() {
    if (!S.messages.length) {
      mount(thread, S.modelReady === false
        ? h('div.info-hint',
            '还没配模型，只能看上面的定义。',
            h('button.btn.btn-ghost.btn-xs', {
              onclick: () => { close(); location.hash = '#/settings'; },
            }, '去设置'))
        : h('div.info-hint.dim', '这一栏只记这个词下面问过的话，跟别的对话分开。'));
      return;
    }
    mount(thread, ...S.messages.map(bubble));
    thread.scrollTop = thread.scrollHeight;
  }

  function paintLast() {
    const last = thread.lastElementChild;
    const msg = S.messages[S.messages.length - 1];
    if (!last || !msg) return paint();
    last.replaceWith(bubble(msg));
    thread.scrollTop = thread.scrollHeight;
  }
}

function bubble(m) {
  const meta = m.meta || {};
  const body = [];
  if (m.content) body.push(...paragraphs(m.content));
  if (m.streaming && !m.content) body.push(h('span.dim', '…'));
  if (meta.aborted) body.push(h('div.dim.small', '（已停止）'));
  if (meta.noModel) body.push(h('div.small', '还没配模型。设置页里粘贴一份配置就能用。'));
  if (meta.error) body.push(h('div.small.danger', meta.error));
  return h(`div.info-msg.is-${m.role}`,
    h('div.info-msg-body', ...body),
    m.created_at ? h('div.info-msg-time.dim', fmtTime(m.created_at)) : null);
}

/**
 * 跟着当前数据走的附加段。
 *
 * 「这张图上出现了哪几档判级、各多少帧」不属于术语表 —— 术语表是所有页面
 * 共用的固定定义，这一段只属于这一张图。所以由调用方传进来，
 * 摆在定义下面、追问上面。
 */
function extraBlock(extra) {
  if (!extra?.items?.length) return null;
  return h('div.info-extra',
    extra.title ? h('div.info-extra-title', extra.title) : null,
    ...extra.items.map((it) => h('div.info-extra-row',
      h('span.mono.info-extra-key', it.key),
      h('div.info-extra-text',
        h('span.strong', it.label),
        it.count != null ? h('span.dim', `　这张图里 ${it.count} 帧`) : null,
        it.note ? h('div.dim.mt-1', it.note) : null))));
}

/** 定义原文和模型的回答走同一个渲染器 —— 术语表里用 `**` 标的重点、
 *  模型吐的反引号，两边都得变成真的粗体和代码，不能一边行一边不行。 */
const paragraphs = (text) => richText(text, 'info-p');
