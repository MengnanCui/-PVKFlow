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
  desc: '让 AI 自己调工具查数据、读写文件、跑批处理 —— 会改东西的操作先问你',
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
  '对 ZG0013 这一批跑一次批处理，跑完告诉我膜厚的范围',
  '把这次的结论写成一份 Markdown 报告，存到 agent/报告.md',
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
  refs.stop = h('button.btn', { style: { display: 'none' }, onclick: () => stopRun() }, '停止');
  refs.modelHost = h('span');
  refs.sessions = h('div.agent-sessions');
  refs.toolList = h('div.agent-tools');
  refs.perm = h('div');
  refs.roots = h('div');
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
        h('div.panel-title', '会改东西的操作'), refs.perm),
      h('div.panel.panel-body',
        h('div.panel-title', '它能碰的文件夹'), refs.roots),
      h('div.panel.panel-body',
        h('div.panel-title', '它能用的工具'),
        h('p.xsmall.dim', '平台的库（样品、结果、样品集）、上面那几个文件夹、批处理和后台任务。不跑任意命令。'),
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
    h('p.small', '它会自己决定先查什么、再查什么，每一步都会摊在这里。会改东西的操作（审核结果、存 / 删样品集、写文件、跑批处理、停任务）执行前会停下来问你；写文件前会自动备份，随时能撤销。'),
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
  drawRoots();
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
    refs.limits.textContent = `一次最多 ${S.limits.max_turns} 轮、${Math.round(S.limits.max_seconds / 60)} 分钟（等批处理也算在里面，`
      + `每次最多等 ${S.limits.task_wait_max} 秒）；每个工具结果最多给模型 ${S.limits.tool_max_chars} 字，`
      + `上下文超过 ${S.limits.compact_at_chars} 字就折叠早期的工具结果。`;
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
    opt('ask', '每次问我', '审核结果、存样品集、写文件、跑批处理、停任务之前停下来，你点了才做'),
    opt('auto', '改得回来的直接做', '上面这些不再问（写文件照样先备份）；删除样品集仍然每次问'),
    h('div.row.gap-2.mt-3', { style: { alignItems: 'center' } }, h('span.small', '模型怎么调工具'), driver),
    h('p.xsmall.dim', '自动：先按标准的工具调用发；网关不认就改成让模型输出一段 JSON 动作，并记住。'
      + '网关认工具调用、但模型用得不好时，可以手动选 JSON 动作协议。'));
}

/** 它能碰的文件夹：工作区总在（里面 raw / config / modules / 数据库只读）；你加的每个单独选只读还是读写。 */
function drawRoots() {
  const roots = S.settings.roots || [];
  const mine = roots.filter((r) => !r.builtin);
  const save = async (next) => {
    try {
      S.settings = await api.saveAgentSettings({ roots: next.map((r) => ({ path: r.path, write: r.write })) });
      drawRoots();
      toast('已保存。下一句话起生效', 'ok');
    } catch (err) { toast(err.message, 'err', 6000); }
  };
  const path = h('input.input.input-sm', { placeholder: '完整路径，如 D:\\数据\\2026', 'aria-label': '要加的文件夹',
                                            style: { width: '100%' } });
  const rw = h('input', { type: 'checkbox', 'aria-label': '允许写' });
  mount(refs.roots,
    ...roots.map((r) => h('div.agent-root-row' + (r.builtin ? '.is-builtin' : ''),
      h('span.mono.xsmall.grow', { title: r.path }, r.builtin ? '工作区' : r.path),
      r.builtin
        ? h('span.tag', { title: 'raw / config / modules / tables / derived / cache 和数据库文件只读' }, '读写·部分只读')
        : h('button.btn.btn-sm.btn-ghost', {
            title: '切换只读 / 读写',
            onclick: () => save(mine.map((x) => (x.path === r.path ? { ...x, write: !x.write } : x))),
          }, r.write ? '读写' : '只读'),
      r.builtin ? null : h('button.icon-btn', {
        title: '不让它再碰这个文件夹', 'aria-label': `移除 ${r.path}`,
        onclick: () => save(mine.filter((x) => x.path !== r.path)),
      }, '✕'))),
    h('div.col.gap-1.mt-2', path,
      h('div.row.gap-2', { style: { alignItems: 'center' } },
        h('label.xsmall.row.gap-1', rw, '允许写'), h('span.grow'),
        h('button.btn.btn-sm', {
          onclick: () => {
            if (!path.value.trim()) { toast('先填文件夹路径', 'err'); return; }
            save([...mine, { path: path.value.trim(), write: rw.checked }]);
          },
        }, '加上'))),
    h('p.xsmall.dim.mt-1', '相对路径从工作区算。写之前会备份到 workspace/agent_backups/，读出来的密钥会先打码再给模型。'));
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

/** 停止：先告诉服务端（正在等批处理的那一步每秒看一眼这个标记），再断开连接。 */
async function stopRun() {
  if (S.sessionId) { try { await api.agentStop(S.sessionId); } catch { /* 断开连接照样能停 */ } }
  S.ctrl?.abort();
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
  // 写了文件：卡片上直接给「撤销这次改动」—— 是你点的，不经过模型、也不用再批
  let out = null;
  try { out = JSON.parse(e.detail || ''); } catch { /* 不是 JSON */ }
  if (e.ok && card.dataset.name === 'write_file' && out?.change_id) {
    const undo = h('button.btn.btn-sm', {
      onclick: async (ev) => {
        ev.stopPropagation();
        try {
          const r = await api.agentRollback(out.change_id);
          if (r.restored?.length) { toast(`已撤销：${r.restored.join('、')}`, 'ok'); undo.disabled = true; undo.textContent = '已撤销'; }
          else toast(r.skipped?.[0] || '没有可撤销的', 'warn', 6000);
        } catch (err) { toast(err.message, 'err', 6000); }
      },
    }, '撤销这次改动');
    card.querySelector('summary').append(h('span.xsmall.dim', out.path), undo);
  }
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
    previewNode(e.preview),
    h('div.row.gap-2.mt-2', { style: { alignItems: 'center' } },
      h('button.btn.btn-sm.btn-primary', { disabled: !active, onclick: () => decide(true) }, '批准'),
      h('button.btn.btn-sm', { disabled: !active, onclick: () => decide(false) }, '拒绝'),
      remember));
  return card;
}

/** 审批卡上的那句话。写文件的预览带差异：+ 行绿、- 行红、@@ 行灰。 */
function previewNode(text) {
  const lines = String(text || '').split('\n');
  const hasDiff = lines.some((l) => l.startsWith('@@ '));
  if (!hasDiff) return h('div.agent-preview.small', text);
  return h('div.agent-preview.small', ...lines.map((l) => h(
    `div${l.startsWith('+ ') ? '.diff-add' : l.startsWith('- ') ? '.diff-del' : l.startsWith('@@') ? '.diff-hunk' : ''}`,
    l || ' ')));
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

