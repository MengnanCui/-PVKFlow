// 智能体：让模型自己调工具查数据、帮你审结果 —— 会改数据的操作先停下来问你。
//
// 和右上角「AI 助手」抽屉的分工：抽屉是「带着当前页面的一段事实问答」，模型只看得到平台塞给它的那些；
// 这里模型自己决定查什么（字段清单 → 相关 → 某个样品的结果……），一轮轮调工具，
// 每一步都摊在界面上（调了什么、参数、用了多久、拿回了什么）。设计照 IFlow 的智能体：
//   · 工具分三级：只读的直接跑；会改数据但改得回来的（审核、存样品集）默认问；删除每次都问
//   · 停在审批上的那一轮存在服务端，点了「批准 / 拒绝」从那儿接着跑；拒绝会告诉模型
//   · 轮数、时间、单个工具结果的大小都有上限，撞上了如实说停在哪
//
// 页面是常驻的：跑着的时候切去别的页再回来，回答还在接着出，不会被打断。

import { api } from '../api.js';
import { h, mount, toast, fmtTime, richText, confirmDialog } from '../ui.js';
import { modelSelect, pickedModel, onModelPicked, syncSelect } from '../components/chat-client.js';

export const meta = {
  id: 'agent',
  title: '智能体',
  desc: '让 AI 自己调工具查数据、帮你审结果 —— 会改数据的操作先问你',
};

const RISK = {
  read: { tag: '只读', cls: 'tag', note: '直接跑' },
  write: { tag: '会改数据', cls: 'tag.tag-warn', note: '先问你；改得回来' },
  irreversible: { tag: '删除', cls: 'tag.tag-danger', note: '每次都问；删了找不回' },
};
const DONE_NOTE = {
  awaiting_approval: '等你确认',
  max_turns: '到了轮数上限，停下来了',
  max_time: '查得太久，停下来了',
  canceled: '已停止',
  failed: '没有完成',
};
const SUGGEST = [
  '现在平台里有什么数据？还有多少结果没审？',
  '有哪些字段？挑两个看起来相关的算一下相关性，告诉我哪些样品偏离趋势',
  '原始文件都还在吗？',
];

const S = {
  tools: [], byName: {}, limits: null, settings: { permission: 'ask' },
  sessionId: null, ctrl: null, live: null, cards: new Map(), decisions: {}, remember: new Set(),
};
const refs = {};

const label = (name) => S.byName[name]?.label || name;

export async function view(host, ctx) {
  refs.nav = ctx.nav;
  if (!refs.root || !host.contains(refs.root)) buildShell(host);
  loadSide();
}

// ------------------------------------------------------------------ 骨架
function buildShell(host) {
  refs.log = h('div.agent-log', { role: 'log', 'aria-live': 'polite' });
  refs.status = h('div.agent-status.xsmall.dim');
  refs.input = h('textarea.input.agent-input', {
    rows: 3, placeholder: '想让它查什么、做什么？（回车发送，Shift+回车换行）',
    'aria-label': '给智能体的话',
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    },
  });
  refs.send = h('button.btn.btn-primary', { onclick: () => send() }, '发送');
  // 用 display 而不是 hidden 属性：.btn 自己的 display 会盖过 [hidden]
  refs.stop = h('button.btn', { style: { display: 'none' }, onclick: () => S.ctrl?.abort() }, '停止');
  refs.modelHost = h('span');
  refs.sessions = h('div.agent-sessions');
  refs.toolList = h('div.agent-tools');
  refs.perm = h('div');
  refs.limits = h('div.xsmall.dim');

  refs.root = h('div.agent-layout',
    h('div.agent-main',
      refs.log,
      h('div.agent-composer',
        refs.input,
        h('div.row.gap-2', { style: { alignItems: 'center' } },
          refs.status, h('span.grow'), refs.modelHost, refs.stop, refs.send))),
    h('aside.agent-side',
      h('div.panel.panel-body',
        h('div.row-between', h('div.panel-title', '会话'),
          h('button.btn.btn-sm', { onclick: () => newSession() }, '新任务')),
        refs.sessions),
      h('div.panel.panel-body',
        h('div.panel-title', '会改数据的操作'), refs.perm),
      h('div.panel.panel-body',
        h('div.panel-title', '它能用的工具'),
        h('p.xsmall.dim', '只碰平台自己的库（样品、结果、样品集）；不碰你电脑上的文件，不跑命令。'),
        refs.toolList, refs.limits)));
  mount(host, refs.root);
  drawEmpty();
  drawModel();
  onModelPicked((v) => syncSelect(refs.modelSel, v));
}

async function drawModel() {
  const { el } = await modelSelect({ title: '用哪个模型' });
  refs.modelSel = el;
  mount(refs.modelHost, el);
}

function drawEmpty() {
  mount(refs.log, h('div.agent-empty',
    h('p.small', '它会自己决定先查什么、再查什么，每一步都会摊在这里。会改数据的操作（审核结果、存样品集、删样品集）执行前会停下来问你。'),
    h('div.agent-suggest', ...SUGGEST.map((t) => h('button.btn.btn-sm.btn-ghost', {
      onclick: () => { refs.input.value = t; refs.input.focus(); },
    }, t)))));
}

// ------------------------------------------------------------------ 侧栏
async function loadSide() {
  try {
    const [t, st] = await Promise.all([
      S.tools.length ? null : api.agentTools(),
      api.agentSettings(),
    ]);
    if (t) {
      S.tools = t.tools; S.limits = t.limits;
      S.byName = Object.fromEntries(t.tools.map((x) => [x.name, x]));
    }
    S.settings = st;
  } catch (err) { toast(err.message, 'err'); }
  drawTools();
  drawPerm();
  drawSessions();
}

function drawTools() {
  mount(refs.toolList, ...S.tools.map((t) => {
    const r = RISK[t.risk] || RISK.read;
    return h('div.agent-tool-row', { title: t.description },
      h('span.small', t.label), h('span.mono.xsmall.dim', t.name), h('span.grow'),
      h(`span.${r.cls}`, r.tag));
  }));
  if (S.limits) {
    refs.limits.textContent = `一次最多 ${S.limits.max_turns} 轮、${Math.round(S.limits.max_seconds / 60)} 分钟；`
      + `每个工具结果最多给模型 ${S.limits.tool_max_chars} 字，上下文超过 ${S.limits.compact_at_chars} 字就折叠早期的工具结果。`;
  }
}

function drawPerm() {
  const opt = (v, title, desc) => h('label.kind-option',
    h('input', {
      type: 'radio', name: 'agent-perm', value: v, checked: S.settings.permission === v,
      onchange: async () => {
        try { S.settings = await api.saveAgentSettings({ permission: v }); toast('已保存', 'ok'); }
        catch (err) { toast(err.message, 'err'); }
      },
    }),
    h('div', h('div.small.strong', title), h('div.xsmall.dim', desc)));
  const driver = h('select.select.select-sm', {
    'aria-label': '模型怎么调工具',
    onchange: async (e) => {
      try { S.settings = await api.saveAgentSettings({ driver: e.target.value }); toast('已保存', 'ok'); }
      catch (err) { toast(err.message, 'err'); }
    },
  }, ...[['auto', '自动判断（推荐）'], ['toolcall', '原生工具调用'], ['action', 'JSON 动作协议']]
    .map(([v, t]) => h('option', { value: v, selected: S.settings.driver === v }, t)));
  mount(refs.perm,
    opt('ask', '每次问我', '审核结果、存样品集之前停下来，你点了才做'),
    opt('auto', '改得回来的直接做', '审核、存样品集不再问；删除样品集仍然每次问'),
    h('div.row.gap-2.mt-3', { style: { alignItems: 'center' } }, h('span.small', '模型怎么调工具'), driver),
    h('p.xsmall.dim', '自动：先按标准的工具调用发；网关不认就改成让模型输出一段 JSON 动作，并记住。'
      + '网关认工具调用、但模型用得不好时，可以手动选 JSON 动作协议。'));
}

async function drawSessions() {
  let list = [];
  try { list = (await api.agentSessions()).sessions; } catch { /* 侧栏取不到不挡主流程 */ }
  if (!list.length) { mount(refs.sessions, h('p.xsmall.dim', '还没有会话。说第一句话就会建一个。')); return; }
  mount(refs.sessions, ...list.map((s) => h('div.agent-session-row' + (s.session_id === S.sessionId ? '.is-current' : ''),
    h('button.linklike.grow', {
      title: s.title, onclick: () => openSession(s.session_id),
    }, s.title),
    s.waiting ? h('span.tag.tag-warn', '等确认') : null,
    h('span.xsmall.dim', fmtTime(s.updated_at)),
    h('button.icon-btn', {
      title: '删除这个会话', 'aria-label': `删除会话「${s.title}」`,
      onclick: async () => {
        if (!await confirmDialog(`删除会话「${s.title}」？只删这段对话记录，它做过的改动不会撤回。`,
          { confirmLabel: '删除', danger: true })) return;
        try {
          await api.deleteAgentSession(s.session_id);
          if (s.session_id === S.sessionId) newSession();
          drawSessions();
        } catch (err) { toast(err.message, 'err'); }
      },
    }, '✕'))));
}

function newSession() {
  if (S.ctrl) { toast('这一轮还在跑，先停下来', 'warn'); return; }
  S.sessionId = null;
  S.cards.clear();
  drawEmpty();
  drawSessions();
  refs.input.focus();
}

async function openSession(id) {
  if (S.ctrl) { toast('这一轮还在跑，先停下来', 'warn'); return; }
  try {
    const s = await api.agentSession(id);
    S.sessionId = id;
    S.cards.clear();
    mount(refs.log);
    const waiting = new Set(s.pending.map((c) => c.id));
    for (const e of s.events) onEvent(e, { replay: true, waiting });
    drawSessions();
    scrollDown();
  } catch (err) { toast(err.message, 'err'); }
}

// ------------------------------------------------------------------ 发送 / 接着跑
async function send() {
  const prompt = refs.input.value.trim();
  if (!prompt || S.ctrl) return;
  if (!S.sessionId) mount(refs.log);
  refs.input.value = '';
  await stream((opts) => api.agentRun({ prompt, session_id: S.sessionId, ...pickedModel() }, opts));
}

async function resume() {
  const body = { decisions: { ...S.decisions }, remember: [...S.remember], ...pickedModel() };
  S.decisions = {};
  S.remember = new Set();
  await stream((opts) => api.agentResume(S.sessionId, body, opts));
}

async function stream(start) {
  S.ctrl = new AbortController();
  busy(true);
  try {
    await start({ signal: S.ctrl.signal, onFrame: (event, data) => onEvent({ ...data, type: event }) });
  } catch (err) {
    if (err.name === 'AbortError') onEvent({ type: 'done', reason: 'canceled' });
    else if (err.kind === 'no_model' || err.status === 501) {
      append(h('div.notice.notice-warn', h('div.grow', '还没有配模型。智能体和 AI 助手用同一份模型设置。'),
        h('button.btn.btn-sm', { onclick: () => refs.nav('settings') }, '去设置')));
    } else append(h('div.notice.notice-warn', h('div.grow', err.message)));
  } finally {
    S.ctrl = null;
    busy(false);
    drawSessions();
  }
}

function busy(on) {
  refs.send.disabled = on;
  refs.stop.style.display = on ? '' : 'none';
  if (!on) refs.status.textContent = '';
}

// ------------------------------------------------------------------ 画事件
function append(node) {
  refs.log.appendChild(node);
  scrollDown();
  return node;
}

function scrollDown() {
  const vp = refs.log.closest('.viewport');
  // 人正往上翻着看的时候不要把他拽下来
  if (!vp || vp.scrollHeight - vp.scrollTop - vp.clientHeight < 240) {
    refs.log.lastElementChild?.scrollIntoView({ block: 'end' });
  }
}

function onEvent(e, { replay = false, waiting = new Set() } = {}) {
  switch (e.type) {
    case 'session':
      S.sessionId = e.session_id;
      break;
    case 'progress':
      if (!replay) refs.status.textContent = e.text;
      break;
    case 'user':
      append(h('div.agent-msg.is-user', e.text));
      break;
    case 'delta':
      if (e.first || !S.live) S.live = append(h('div.agent-msg.is-bot.is-live'));
      S.live.textContent += e.text;
      scrollDown();
      break;
    case 'text': {
      const node = h('div.agent-msg.is-bot', ...richText(e.text));
      if (S.live) { S.live.replaceWith(node); S.live = null; } else append(node);
      break;
    }
    case 'tool_call':
      S.live?.remove(); S.live = null;
      S.cards.set(e.id, append(toolCard(e)));
      break;
    case 'tool_result':
      fillCard(S.cards.get(e.id), e);
      break;
    case 'approval': {
      const tc = S.cards.get(e.id);
      if (tc) tc.__status.textContent = '等你确认';
      append(approvalCard(e, replay ? waiting.has(e.id) : true));
      break;
    }
    case 'decision':
      settleApproval(e);
      break;
    case 'error':
      S.live = null;
      append(h('div.notice.notice-warn', h('div.grow', e.message)));
      break;
    case 'done': {
      S.live = null;
      const note = DONE_NOTE[e.reason];
      const bits = [note, e.turns ? `${e.turns} 轮` : '', e.model || '', e.driver === 'action' ? '动作协议' : '']
        .filter(Boolean);
      if (bits.length) append(h(`div.agent-done.xsmall${e.reason === 'finished' ? '.dim' : ''}`, bits.join(' · ')));
      break;
    }
    default:
  }
}

function toolCard(e) {
  const r = RISK[e.risk] || RISK.read;
  const status = h('span.agent-tool-status.xsmall.dim', '进行中…');
  const body = h('div.agent-tool-body',
    h('div.xsmall.dim', '参数'),
    h('pre.agent-pre', JSON.stringify(e.args ?? {}, null, 2)));
  const card = h('details.agent-tool', { dataset: { id: e.id, name: e.name } },
    h('summary',
      h('span.agent-tool-icon', '⚙'),
      h('span.small', label(e.name)), h('span.mono.xsmall.dim', e.name),
      e.risk !== 'read' ? h(`span.${r.cls}`, r.tag) : null,
      h('span.grow'), status),
    body);
  card.__status = status;
  card.__body = body;
  return card;
}

function fillCard(card, e) {
  if (!card) return;
  card.classList.add(e.ok ? 'is-ok' : 'is-fail');
  card.__status.textContent = `${e.ok ? '✓' : '✗'} ${e.ms} ms`;
  card.__status.title = e.summary;
  card.__body.append(
    h('div.xsmall.dim.mt-2', e.ok ? '拿回来的（给模型的原文）' : '出错了（原样告诉了模型，让它自己改）'),
    h('pre.agent-pre', pretty(e.detail || e.summary)));
}

const pretty = (text) => {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
};

function approvalCard(e, active) {
  const r = RISK[e.risk] || RISK.write;
  const remember = e.risk === 'write'
    ? h('label.xsmall.row.gap-1', h('input', { type: 'checkbox', disabled: !active }),
        `本次会话内不再问「${label(e.name)}」`)
    : h('span.xsmall.dim', '删除每次都要问，记不住');
  const decide = (ok) => {
    S.decisions[e.id] = ok ? 'approve' : 'reject';
    if (ok && remember.querySelector?.('input')?.checked) S.remember.add(e.name);
    for (const b of card.querySelectorAll('button')) b.disabled = true;
    card.querySelector('.agent-approval-state').textContent = ok ? '已批准，正在执行…' : '已拒绝';
    // 这一轮停下来的调用都有了答复，才接着跑（漏了的服务端按拒绝处理）
    const open = [...refs.log.querySelectorAll('.agent-approval.is-open')].filter((x) => !(x.dataset.id in S.decisions));
    if (!open.length) resume();
  };
  const card = h(`div.agent-approval${active ? '.is-open' : ''}`, { dataset: { id: e.id }, role: 'group', 'aria-label': `要不要执行：${label(e.name)}` },
    h('div.row.gap-2', { style: { alignItems: 'center' } },
      h('span.strong.small', `要执行「${label(e.name)}」吗？`), h(`span.${r.cls}`, r.tag), h('span.grow'),
      h('span.agent-approval-state.xsmall.dim', active ? '' : '（这一轮已经过去了）')),
    h('div.agent-preview.small', e.preview),
    h('div.row.gap-2.mt-2', { style: { alignItems: 'center' } },
      h('button.btn.btn-sm.btn-primary', { disabled: !active, onclick: () => decide(true) }, '批准'),
      h('button.btn.btn-sm', { disabled: !active, onclick: () => decide(false) }, '拒绝'),
      remember));
  return card;
}

function settleApproval(e) {
  const card = refs.log.querySelector(`.agent-approval[data-id="${CSS.escape(e.id)}"]`);
  const text = e.stale ? '没批就说了下一句，没有执行'
    : e.decision === 'approved' ? `已批准${e.auto ? '（设置里选了直接做）' : e.remembered ? '（这个会话里不再问）' : ''}`
      : '已拒绝，没有执行';
  const tc = S.cards.get(e.id);
  if (tc && e.decision !== 'approved') {
    tc.classList.add('is-fail');
    tc.__status.textContent = '✗ 没执行';
  }
  if (card) {
    card.classList.remove('is-open');
    for (const b of card.querySelectorAll('button, input')) b.disabled = true;
    card.querySelector('.agent-approval-state').textContent = text;
    card.classList.add(e.decision === 'approved' ? 'is-approved' : 'is-rejected');
  } else if (e.decision === 'approved') {
    // 自动执行 / 记住了的：没有审批卡，在工具卡上标一下
    tc?.querySelector('summary')?.append(h('span.xsmall.dim', `· ${text}`));
  }
}

