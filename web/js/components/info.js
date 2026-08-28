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

import { h, mount, modal, toast, fmtTime, richText } from '../ui.js';
import { api } from '../api.js';
import { term } from '../glossary.js';

/**
 * 造一个 ⓘ 按钮。
 *
 * @param {string} id  glossary.js 里的术语 id
 * @param {{label?: string}} opts  label 只影响无障碍标签，不显示
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
  return h('button.info-dot', {
    type: 'button',
    title: `${t.title} —— 点开看说明`,
    'aria-label': `${opts.label || t.title} 的说明`,
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); openInfo(id); },
  }, 'ⓘ');
}

/** 一行「词 + ⓘ」。给标题、图注、表头用，省得每处都写一遍 flex。 */
export function withInfo(text, id) {
  const dot = infoDot(id, { label: typeof text === 'string' ? text : undefined });
  return dot ? h('span.with-info', text, dot) : h('span', text);
}

// ------------------------------------------------------------------ 弹窗
const TOPIC = (id) => `glossary:${id}`;

export function openInfo(id) {
  const t = term(id);
  if (!t) return;

  const S = { conv: null, messages: [], streaming: false, abort: null, modelReady: null };

  const thread = h('div.info-thread');
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

  const m = modal({
    title: t.title,
    width: 'min(680px, 94vw)',
    body: h('div.info-wrap',
      // ── 上半截：固定定义。这一段不受模型影响。
      h('div.info-def',
        h('div.info-what', t.what),
        h('div.info-why', ...paragraphs(t.why))),
      // ── 下半截：接着问
      h('div.info-chat',
        h('div.info-chat-head',
          h('span.dim', '接着问'),
          h('button.btn.btn-ghost.btn-xs', {
            onclick: () => clearThread(),
            title: '清掉这个词下面问过的话',
          }, '清空')),
        thread,
        h('div.info-compose', input, sendBtn))),
    onClose: () => S.abort?.abort(),
  });

  paint();
  loadHistory();
  probe();

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
      const s = await api.assistStatus();
      S.modelReady = !!s.model_configured;
    } catch { S.modelReady = false; }
    if (!S.messages.length) paint();
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
              onclick: () => { m.close(); location.hash = '#/settings'; },
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

/** 定义原文和模型的回答走同一个渲染器 —— 术语表里用 `**` 标的重点、
 *  模型吐的反引号，两边都得变成真的粗体和代码，不能一边行一边不行。 */
const paragraphs = (text) => richText(text, 'info-p');
