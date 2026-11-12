// 智能体：一个 pi-agent 式的轻量对话窗口（P-53）。
//
// 一栏到底：顶上一条细栏（会话、权限「询问 / 自动」、模式「直接执行 / 先出计划」、模型、⚙），
// 中间是对话 —— 每次调工具是一行（▸ 工具 · 关键参数 · ✓ 用时，点开看参数和原文），
// 底下一个输入框，支持斜杠命令（/plan /exec /ask /auto /new /undo /stop /help）。
// 权限、文件夹、工具清单这些不常动的东西收进 ⚙ 抽屉，不占地方。
//
// 设计照 IFlow 的智能体：工具分三级（只读直接跑 / 会改东西的按权限问 / 删除和危险命令每次问）；
// 停在审批上的那一轮存在服务端；计划模式只调研、交计划，批准后再按计划执行。
// 页面是常驻的：跑着的时候切去别的页再回来，回答还在接着出。

import { api } from '../api.js';
import { h, mount, toast, fmtTime, richText, confirmDialog, segmented, errorBox } from '../ui.js';
import { modelSelect, pickedModel, onModelPicked, syncSelect } from '../components/chat-client.js';

export const meta = {
  id: 'agent',
  title: '智能体',
  desc: '查数据、读写文件、跑命令和批处理 —— '
    + '写的东西都在专用目录里，会改东西的操作按你的设置先问',
};

const RISK = {
  read: { tag: '只读', cls: 'tag', note: '直接跑' },
  write: { tag: '会改东西', cls: 'tag.tag-warn', note: '按权限设置问你；改得回来' },
  irreversible: { tag: '每次都问', cls: 'tag.tag-danger', note: '删除 / 危险命令：每次都问' },
};
const DONE_NOTE = {
  awaiting_approval: '等你确认',
  plan_ready: '计划交出来了，等你批准',
  max_turns: '到了轮数上限，停下来了',
  max_time: '查得太久，停下来了',
  canceled: '已停止',
  failed: '没有完成',
};
const HELP = [
  ['/plan', '切到「先出计划」：只调研、交计划，你批准了才动手'],
  ['/exec', '切到「直接执行」'],
  ['/ask', '会改东西的操作每次问我'],
  ['/auto', '会改东西的直接做（删除、危险命令仍然问）'],
  ['/new', '开一个新会话'],
  ['/undo', '撤销这个会话里智能体改过的全部文件'],
  ['/stop', '停下正在跑的这一轮'],
  ['/delete', '删掉这个会话（问你一次）'],
  ['/help', '看这张表'],
];
// 每种工具在一行里显示哪个参数（pi 那样：一眼看出它在干什么）
const KEY_ARG = {
  run_command: 'command', read_file: 'path', write_file: 'path', list_files: 'path', search_files: 'pattern',
  get_task: 'task_id', cancel_task: 'task_id', batch_summary: 'run_id', sample_results: 'sample_id',
  find_samples: 'q', correlate: 'x', compare_batches: 'field', propose_plan: 'goal', rollback_file: 'change_id',
};

const S = {
  tools: [], byName: {}, limits: null, settings: { permission: 'ask', commands: true, roots: [] },
  runMode: 'exec', sessionId: null, ctrl: null, live: null, cards: new Map(), decisions: {}, remember: new Set(),
};
const refs = {};

const label = (name) => S.byName[name]?.label || name;
const RUN_MODE_KEY = 'hte.agent.runMode';
try { S.runMode = localStorage.getItem(RUN_MODE_KEY) === 'plan' ? 'plan' : 'exec'; } catch { /* 隐私模式 */ }

export async function view(host, ctx) {
  refs.nav = ctx.nav;
  if (!refs.root || !host.contains(refs.root)) buildShell(host);
  loadSide();
}

// ------------------------------------------------------------------ 骨架
function buildShell(host) {
  refs.log = h('div.agent-log', { role: 'log', 'aria-live': 'polite' });
  refs.status = h('span.agent-status.xsmall.dim');
  refs.input = h('textarea.input.agent-input', {
    rows: 2, placeholder: '说要做什么…（回车发送，Shift+回车换行，/ 看命令）',
    'aria-label': '给智能体的话',
    onkeydown: (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    },
  });
  refs.send = h('button.btn.btn-primary.btn-sm', { onclick: () => send() }, '发送');
  // 用 display 而不是 hidden 属性：.btn 自己的 display 会盖过 [hidden]
  refs.stop = h('button.btn.btn-sm', { style: { display: 'none' }, onclick: () => stopRun() }, '停止');
  refs.modelHost = h('span');
  refs.sessionSel = h('select.select.select-sm.agent-session-sel', {
    'aria-label': '会话', onchange: (e) => { if (e.target.value) openSession(e.target.value); else newSession(); },
  });
  refs.perm = segmented('权限', S.settings.permission, [
    ['ask', '询问', '会改东西的操作（写文件、跑命令、跑批处理……）每次先问我'],
    ['auto', '自动', '会改东西的直接做；删除、危险命令仍然每次问'],
  ], (v) => setPermission(v));
  refs.mode = segmented('模式', S.runMode, [
    ['exec', '直接执行', '说了就做'],
    ['plan', '先出计划', '只调研、交计划，你批准了才动手'],
  ], (v) => setRunMode(v));
  refs.drawer = h('div.agent-drawer', { hidden: true });
  refs.foot = h('div.agent-foot.xsmall.dim');

  refs.root = h('div.agent-shell',
    h('div.agent-bar',
      refs.sessionSel,
      h('button.btn.btn-sm.btn-ghost', { title: '新会话（/new）', onclick: () => newSession() }, '＋'),
      h('span.agent-bar-gap'),
      refs.perm, refs.mode, refs.modelHost,
      h('button.btn.btn-sm.btn-ghost', {
        title: '权限、文件夹、跑命令、工具清单', 'aria-label': '设置',
        onclick: () => { refs.drawer.hidden = !refs.drawer.hidden; },
      }, '⚙')),
    refs.drawer,
    refs.log,
    h('div.agent-composer',
      refs.input,
      h('div.row.gap-2', { style: { alignItems: 'center' } }, refs.status, h('span.grow'), refs.stop, refs.send),
      refs.foot));
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
    h('p.small', '它会自己查数据、读写文件、跑命令和批处理，每一步都摊在这里。',
      '它写的脚本、报告都放在专用目录 workspace/agent/ 里；平台本身（网站的核心）它改不了。'),
    h('div.agent-suggest', ...[
      '现在平台里有什么数据？还有多少结果没审？',
      '写一个脚本把各批次的膜厚画成箱线图，存到 agent/ 里并运行',
      '对 ZG0013 这一批跑一次批处理，跑完告诉我膜厚的范围',
    ].map((t) => h('button.btn.btn-sm.btn-ghost', {
      onclick: () => { refs.input.value = t; refs.input.focus(); },
    }, t))),
    h('p.xsmall.dim',
      '输入 / 看命令：/plan 先出计划，/exec 直接执行，/ask 每次问，/auto 自动，/undo 撤销它改的文件。')));
}

function drawFoot() {
  const st = S.settings;
  refs.foot.textContent = [
    `写：${st.agent_dir ? 'workspace/agent/' : '专用目录'}`
      + `${(st.roots || []).some((r) => r.kind === 'user' && r.write) ? ' + 你给的文件夹' : ''}`,
    `跑命令：${st.commands ? '开' : '关'}`,
    `权限：${st.permission === 'auto' ? '自动' : '询问'}`,
    `模式：${S.runMode === 'plan' ? '先出计划' : '直接执行'}`,
  ].join(' · ');
}

// ------------------------------------------------------------------ 设置
async function loadSide() {
  try {
    const [t, st] = await Promise.all([S.tools.length ? null : api.agentTools(), api.agentSettings()]);
    if (t) {
      S.tools = t.tools; S.limits = t.limits;
      S.byName = Object.fromEntries(t.tools.map((x) => [x.name, x]));
    }
    S.settings = st;
  } catch (err) { toast(err.message, 'err'); }
  refs.perm.set(S.settings.permission);
  drawDrawer();
  drawFoot();
  drawSessions();
}

async function saveSettings(patch, msg = '已保存') {
  try {
    S.settings = await api.saveAgentSettings(patch);
    toast(msg, 'ok');
  } catch (err) { toast(err.message, 'err', 6000); }
  refs.perm.set(S.settings.permission);
  drawDrawer();
  drawFoot();
}

const setPermission = (v) => saveSettings({ permission: v },
  v === 'auto' ? '自动：会改东西的直接做（删除、危险命令仍然问）' : '询问：会改东西的每次先问你');

function setRunMode(v) {
  S.runMode = v;
  refs.mode.set(v);
  try { localStorage.setItem(RUN_MODE_KEY, v); } catch { /* 隐私模式 */ }
  drawFoot();
  toast(v === 'plan' ? '先出计划：只调研、交计划，你批准了才动手' : '直接执行', 'info');
}

/** ⚙ 抽屉：权限一览（读 / 写 / 跑命令 / 核心）、文件夹、调用方式、工具清单。 */
function drawDrawer() {
  const st = S.settings;
  const cmd = h('label.small.row.gap-1', h('input', {
    type: 'checkbox', checked: !!st.commands, onchange: (e) => saveSettings({ commands: e.target.checked },
      e.target.checked ? '已允许跑命令' : '已关掉跑命令'),
  }), '允许跑命令');
  const driver = h('select.select.select-sm', {
    'aria-label': '模型怎么调工具',
    onchange: (e) => saveSettings({ driver: e.target.value }),
  }, ...[['auto', '自动判断（推荐）'], ['toolcall', '原生工具调用'], ['action', 'JSON 动作协议']]
    .map(([v, t]) => h('option', { value: v, selected: st.driver === v }, t)));
  refs.roots = h('div');
  mount(refs.drawer,
    h('div.agent-drawer-grid',
      h('div',
        h('div.small.strong', '权限'),
        h('ul.xsmall.agent-perm-list',
          h('li', h('b', '写：'), '只在专用目录 ', h('span.mono', st.agent_dir || 'workspace/agent'),
            '（它的脚本、报告、中间数据），以及下面你给了「读写」的文件夹'),
          h('li', h('b', '读：'), '整个工作区 + 下面的文件夹；读出来的密钥先打码再给模型'),
          h('li', h('b', '核心：'),
            '平台的安装目录（网站本身）任何时候都不能写；跑命令后还会检查核心有没有被改'),
          h('li', h('b', '跑命令：'),
            '在专用目录里跑，超时 / 停止会杀掉整棵进程树；删除、装依赖这类每次都问'),
          h('li', h('b', '问不问：'), '顶上「询问 / 自动」；写文件前都会备份，/undo 一键撤销')),
        h('div.row.gap-3.mt-2', { style: { alignItems: 'center' } }, cmd,
          h('span.small', '调用方式'), driver)),
      h('div', h('div.small.strong', '它能碰的文件夹'), refs.roots),
      h('div',
        h('div.small.strong', '工具'),
        h('div.agent-tools', ...S.tools.map((t) => {
          const r = RISK[t.risk] || RISK.read;
          return h('div.agent-tool-row', { title: t.description },
            h('span.small', t.label), h('span.mono.xsmall.dim', t.name), h('span.grow'),
            t.plan_only ? h('span.tag', '计划模式') : h(`span.${r.cls}`, r.tag));
        })),
        S.limits ? h('p.xsmall.dim', `一次最多 ${S.limits.max_turns} 轮、${Math.round(S.limits.max_seconds / 60)} 分钟；`
          + `每个工具结果最多给模型 ${S.limits.tool_max_chars} 字。`) : null)));
  drawRoots();
}

/**
 * 文件夹：专用目录（可写）、工作区（只读）总在；
 * 你加的每个单独选只读还是读写（平台核心只能只读）。
 */
function drawRoots() {
  const roots = S.settings.roots || [];
  const mine = roots.filter((r) => r.kind === 'user');
  const save = (next) => saveSettings({ roots: next.map((r) => ({ path: r.path, write: r.write })) }, '已保存。下一句话起生效');
  const path = h('input.input.input-sm', { placeholder: '完整路径，如 D:\\数据\\2026', 'aria-label': '要加的文件夹',
                                            style: { width: '100%' } });
  const rw = h('input', { type: 'checkbox', 'aria-label': '允许写' });
  const name = (r) => (r.kind === 'agent' ? '专用目录 agent/' : r.kind === 'workspace' ? '工作区' : r.path);
  mount(refs.roots,
    ...roots.map((r) => h(`div.agent-root-row${r.kind !== 'user' ? '.is-builtin' : ''}`,
      h('span.mono.xsmall.grow', { title: r.path }, name(r)),
      r.kind !== 'user'
        ? h('span.tag', r.write ? '读写' : '只读')
        : h('button.btn.btn-sm.btn-ghost', {
            title: '切换只读 / 读写',
            onclick: () => save(mine.map((x) => (x.path === r.path ? { ...x, write: !x.write } : x))),
          }, r.write ? '读写' : '只读'),
      r.kind !== 'user' ? null : h('button.icon-btn', {
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
        }, '加上'))));
}

// ------------------------------------------------------------------ 会话
async function drawSessions() {
  let list = [];
  try { list = (await api.agentSessions()).sessions; } catch { /* 取不到不挡主流程 */ }
  mount(refs.sessionSel,
    h('option', { value: '' }, S.sessionId ? '＋ 新会话' : '新会话'),
    ...list.map((s) => h('option', { value: s.session_id, selected: s.session_id === S.sessionId },
      `${s.waiting ? '⏸ ' : ''}${s.title} · ${fmtTime(s.updated_at)}`)));
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
  if (S.ctrl) { toast('这一轮还在跑，先停下来', 'warn'); drawSessions(); return; }
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

async function deleteSession() {
  if (!S.sessionId) return;
  if (!await confirmDialog('删掉这个会话？只删对话记录，它做过的改动不会撤回（要撤回文件先 /undo）。',
    { confirmLabel: '删除', danger: true })) return;
  try { await api.deleteAgentSession(S.sessionId); newSession(); } catch (err) { toast(err.message, 'err'); }
}

// ------------------------------------------------------------------ 斜杠命令
async function slash(cmd) {
  const [name] = cmd.trim().split(/\s+/);
  switch (name) {
    case '/plan': setRunMode('plan'); return true;
    case '/exec': setRunMode('exec'); return true;
    case '/ask': setPermission('ask'); return true;
    case '/auto': setPermission('auto'); return true;
    case '/new': newSession(); return true;
    case '/stop': stopRun(); return true;
    case '/delete': deleteSession(); return true;
    case '/undo': {
      if (!S.sessionId) { toast('还没有会话', 'warn'); return true; }
      if (!await confirmDialog('撤销这个会话里智能体改过的全部文件？（从备份还原，新建的删掉）',
        { confirmLabel: '撤销' })) return true;
      try {
        const r = await api.agentUndoSession(S.sessionId);
        append(h('div.agent-done.xsmall', r.restored.length ? `已撤销：${r.restored.join('、')}` : '没有可撤销的文件改动',
          r.skipped.length ? `；没撤：${r.skipped.join('、')}` : ''));
      } catch (err) { toast(err.message, 'err', 6000); }
      return true;
    }
    case '/help': case '/':
      append(h('div.agent-help.xsmall', ...HELP.map(([c, d]) => h('div', h('span.mono', c), '　', d))));
      return true;
    default:
      toast(`不认识的命令：${name}（/help 看全部）`, 'warn');
      return true;
  }
}

// ------------------------------------------------------------------ 发送 / 接着跑
async function send(override) {
  const prompt = (override ?? refs.input.value).trim();
  if (!prompt || S.ctrl) return;
  if (!override) refs.input.value = '';
  if (prompt.startsWith('/') && !override) { await slash(prompt); return; }
  if (!S.sessionId) mount(refs.log);
  await stream((opts) => api.agentRun({ prompt, session_id: S.sessionId, run_mode: S.runMode, ...pickedModel() }, opts));
}

/** 批准计划：按这份计划直接执行（这一轮不管顶上是不是「先出计划」）。 */
async function runPlan(plan) {
  if (S.ctrl) return;
  await stream((opts) => api.agentRun({ prompt: '按计划执行', session_id: S.sessionId, run_mode: 'exec', plan,
                                        ...pickedModel() }, opts));
}

async function resume() {
  const body = { decisions: { ...S.decisions }, remember: [...S.remember], ...pickedModel() };
  S.decisions = {};
  S.remember = new Set();
  await stream((opts) => api.agentResume(S.sessionId, body, opts));
}

async function stream(start) {
  S.ctrl = new AbortController();
  setRunning(true);
  try {
    await start({ signal: S.ctrl.signal, onFrame: (event, data) => onEvent({ ...data, type: event }) });
  } catch (err) {
    if (err.name === 'AbortError') onEvent({ type: 'done', reason: 'canceled' });
    else if (err.kind === 'no_model' || err.status === 501) {
      append(h('div.notice.notice-warn', h('div.grow', '还没有配模型。智能体和 AI 助手用同一份模型设置。'),
        h('button.btn.btn-sm', { onclick: () => refs.nav('settings') }, '去设置')));
    } else append(errorBox(err));   // 和别的页面同一个报错框（原来是黄色的提醒框，C076）
  } finally {
    S.ctrl = null;
    setRunning(false);
    drawSessions();
  }
}

/** 停止：先告诉服务端（等批处理、跑命令的那一步每 0.1–1 秒看一眼这个标记），再断开连接。 */
async function stopRun() {
  if (!S.ctrl) return;
  if (S.sessionId) { try { await api.agentStop(S.sessionId); } catch { /* 断开连接照样能停 */ } }
  S.ctrl?.abort();
}

/**
 * 正在跑：发送键不能点、显示「停止」。
 * 原来叫 busy，和 ui.busy(按钮, 开关) 重名但意思不一样（C076）。
 */
function setRunning(on) {
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
      append(h('div.agent-msg.is-user', e.run_mode === 'plan' ? h('span.tag', { style: { marginRight: '6px' } }, '先出计划') : null,
        e.approved_plan ? h('span.tag.tag-accent', { style: { marginRight: '6px' } }, '按计划执行') : null, e.text));
      break;
    case 'plan':
      append(planCard(e.plan, !replay));
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
      append(errorBox(e));
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
  const key = keyArg(e);
  const card = h('details.agent-tool', { dataset: { id: e.id, name: e.name } },
    h('summary',
      h('span.agent-tool-icon', '▸'),
      h('span.small.strong', label(e.name)),
      key ? h('span.mono.xsmall.agent-key', { title: key }, key) : h('span.mono.xsmall.dim', e.name),
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
          if (r.restored?.length) {
            toast(`已撤销：${r.restored.join('、')}`, 'ok'); undo.disabled = true; undo.textContent = '已撤销';
          }
          else toast(r.skipped?.[0] || '没有可撤销的', 'warn', 6000);
        } catch (err) { toast(err.message, 'err', 6000); }
      },
    }, '撤销这次改动');
    card.querySelector('summary').append(h('span.xsmall.dim', out.path), undo);
  }
}

function keyArg(e) {
  const a = e.args || {};
  const v = a[KEY_ARG[e.name]] ?? Object.values(a).find((x) => typeof x === 'string');
  return v === undefined || v === null || v === '' ? '' : String(v).replace(/\s+/g, ' ').slice(0, 120);
}

/**
 * 计划卡：目标、步骤、风险、要你先回答的；
 * 「按计划执行」就照它做，「改一改」把它填回输入框。
 */
function planCard(plan, active) {
  const go = h('button.btn.btn-sm.btn-primary', {
    disabled: !active,
    onclick: () => { go.disabled = true; edit.disabled = true; go.textContent = '正在按计划执行…'; runPlan(plan); },
  }, '按计划执行');
  const edit = h('button.btn.btn-sm', {
    disabled: !active,
    onclick: () => {
      refs.input.value = `计划要改：\n${plan.steps.map((s, i) => `${i + 1}. ${s.title}`).join('\n')}\n（在这里写要改什么）`;
      refs.input.focus();
    },
  }, '改一改');
  return h('div.agent-plan', { role: 'group', 'aria-label': '执行计划' },
    h('div.small.strong', `计划：${plan.goal}`),
    h('ol.small', ...plan.steps.map((s) => h('li', s.title,
      s.tool || s.change
        ? h('span.xsmall.dim', `（${[s.tool && `用 ${label(s.tool)}`, s.change].filter(Boolean).join('；')}）`)
        : null))),
    plan.risks?.length ? h('div.xsmall', h('b', '风险：'), plan.risks.join('；')) : null,
    plan.questions?.length ? h('div.xsmall', h('b', '要你先回答：'), plan.questions.join('；')) : null,
    h('div.row.gap-2.mt-2', go, edit, h('span.xsmall.dim', active
      ? '批准后按这份计划执行；会改东西的操作照样按顶上的「询问 / 自动」'
      : '（这份计划已经过去了）')));
}

const pretty = (text) => {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
};

function approvalCard(e, active) {
  const r = RISK[e.risk] || RISK.write;
  const remember = e.risk === 'write'
    ? h('label.xsmall.row.gap-1', h('input', { type: 'checkbox', disabled: !active }),
        `本次会话内不再问「${label(e.name)}」`)
    : h('span.xsmall.dim', '这类每次都要问，记不住');
  const decide = (ok) => {
    S.decisions[e.id] = ok ? 'approve' : 'reject';
    if (ok && remember.querySelector?.('input')?.checked) S.remember.add(e.name);
    for (const b of card.querySelectorAll('button')) b.disabled = true;
    card.querySelector('.agent-approval-state').textContent = ok ? '已批准，正在执行…' : '已拒绝';
    // 这一轮停下来的调用都有了答复，才接着跑（漏了的服务端按拒绝处理）
    const open = [...refs.log.querySelectorAll('.agent-approval.is-open')].filter((x) => !(x.dataset.id in S.decisions));
    if (!open.length) resume();
  };
  const card = h(`div.agent-approval${active ? '.is-open' : ''}`,
    { dataset: { id: e.id, name: e.name }, role: 'group', 'aria-label': `要不要执行：${label(e.name)}` },
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
    : e.decision === 'approved'
      ? `已批准${e.auto ? '（设置里选了直接做）' : e.remembered ? '（这个会话里不再问）' : ''}`
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

