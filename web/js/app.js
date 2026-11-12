// 应用外壳：导航、主题、健康检查。

import { api } from './api.js';
import { h, mount, $ } from './ui.js';

import * as overview from './pages/overview.js';
import * as process_ from './pages/process.js';
import * as sample from './pages/sample.js';
import * as batchPage from './pages/batch.js';
import * as history from './pages/history.js';
import * as storage from './pages/storage.js';
import * as relation from './pages/relation.js';
import * as settings from './pages/settings.js';
import * as agent from './pages/agent.js';
import * as qfls from './pages/qfls.js';
import * as qflsPoint from './pages/qfls-point.js';
import { initDrawer } from './components/ai-drawer.js';

const PAGES = [overview, process_, sample, batchPage, history, storage, relation, qfls, qflsPoint, agent, settings];
const BY_ID = Object.fromEntries(PAGES.map((p) => [p.meta.id, p]));

// sample 不出现在侧栏 —— 它是从数据处理页下钻进去的。
//
// 三组是有先后的：「平台」是数据的正向流程（处理 → 存储 → 构效关系），
// 「分析」是回头看已经跑过的东西。对比历史属于后者，跟前面三个不是一个层级，
// 所以单独成组排在后面，而不是塞进主线里当第三项。
const MAIN_NAV = ['overview', 'process', 'storage', 'relation'];
const ANALYSIS_NAV = ['history', 'qfls', 'agent'];
const SYSTEM_NAV = ['settings'];

const NAV_PAGES = new Set([...MAIN_NAV, ...ANALYSIS_NAV, ...SYSTEM_NAV]);

const viewport = $('#viewport');
const mounted = new Map();
let current = null;
let currentArg = null;
// 下钻页（样品、批处理结果、QFLS 样点）「返回」回哪：**你是从哪来的就回哪**，不是写死的父页。
// 原来样品页固定回数据处理 —— 从构效关系、存储页、对比页点进来的也被送回数据处理（技术债 C077）。
// 直接打开链接 / 刷新时没有来处，退回页面声明的 meta.parent（和 meta.parentArg）。
const backOf = new Map();
// 当前这一次「进入页面」的生命周期。离开（导航到任何地方，包括同一页换参数）时 abort ——
// 页面里还没回来的请求、还在跑的轮询、挂在外面的监听，凭它知道自己该停了。
//
// 原来没有这一层：离开对比页后轮询照跑，跑完把别的页面的标题改掉；
// 拖拽上传的监听每进一次页面多挂一份，拖一个文件传 N 遍（自查 §6 #4 #5 #6）。
let life = null;
// 我们自己刚写进地址栏的 hash。写 location.hash 会触发 hashchange，
// 回调里认出它就不再渲染第二遍 —— 原来每次点导航，页面都渲染两次、接口全部翻倍。
let selfWritten = null;

// ------------------------------------------------------------------ 主题
const THEME_KEY = 'hte.theme';
function applyTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* 隐私模式下会抛，忽略 */ }
}
// 首屏的主题由 index.html 里那段内联脚本定（必须在样式表之前，否则闪一帧）。
// 这里不再重复设一遍，**也不再在首次访问时就把值写进 localStorage** ——
// 写了的话系统主题以后再变，界面就不跟了。只有你自己点了切换才记住。
$('#themeToggle').addEventListener('click', () => {
  applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
});

// ------------------------------------------------------------------ 导航
function buildNav(hostId, ids, heading) {
  const host = $(hostId);
  mount(host,
    heading ? h('div.nav-heading', heading) : null,
    ...ids.map((id) => h('button.nav-item', {
      dataset: { page: id },
      onclick: () => navigate(id),
    }, h('span.grow', BY_ID[id].meta.title))));
}

/**
 * navigate('process') 或 navigate('sample', {arg: 'art_xxx'})
 * hash 形如 #sample/art_xxx —— 下钻页面可以直接分享链接、可以刷新、可以后退。
 */
function navigate(id, { push = true, arg = null, back = false } = {}) {
  const page = BY_ID[id];
  if (!page) return navigate('overview');

  // 记下来处。按「返回」回去的那一步不记 —— 不然样品 → 对比页 → 返回样品之后，样品的「返回」就成了对比页，来回打转。
  // 同一页换参数（下一个样点、换一个样品）沿用原来的来处
  if (page.meta.parent && !back && current && current !== id) backOf.set(id, { id: current, arg: currentArg });
  current = id;
  currentArg = arg;
  // 下钻页面把来处那一项保持高亮（顺着来处往上找到侧栏里有的那一页），用户才不会觉得自己「掉出去」了
  const highlight = navOwner(id);
  document.querySelectorAll('.nav-item').forEach((b) => {
    if (b.dataset.page === highlight) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });

  $('#pageTitle').textContent = page.meta.title;
  $('#pageDesc').textContent = page.meta.desc || '';
  // 下钻页的「返回」统一放在页头右上角第一个（原来三个下钻页各放各的：页头、内容区、写死的目标）
  mount($('#pageActions'), page.meta.parent ? backButton(id) : null, ...(page.actions?.(navigate, arg) || []));

  let host = mounted.get(id);
  if (!host) {
    host = h(`section.page#page-${id}`);
    viewport.appendChild(host);
    mounted.set(id, host);
  }
  for (const [pid, node] of mounted) node.classList.toggle('is-active', pid === id);

  life?.abort();
  life = new AbortController();
  const { signal } = life;

  // 每次进入都重新取数 —— 数据会变，缓存一份旧的更糟
  page.view(host, { nav: navigate, arg, signal, alive: () => !signal.aborted });

  const target = arg ? `${id}/${arg}` : id;
  if (push && location.hash.slice(1) !== target) {
    selfWritten = target;
    location.hash = target;
  }
  document.title = `${page.meta.title} · HTE Studio`;
  viewport.scrollTop = 0;
}

function backTarget(id) {
  const meta = BY_ID[id].meta;
  return backOf.get(id) || { id: meta.parent, arg: meta.parentArg || null };
}

/** 侧栏里该亮哪一项：自己在侧栏里就是自己；下钻页顺着来处往上找（最多几层，防环）。 */
function navOwner(id) {
  for (let i = 0, cur = id; i < 6 && cur; i += 1) {
    if (NAV_PAGES.has(cur)) return cur;
    cur = BY_ID[cur]?.meta.parent ? backTarget(cur).id : null;
  }
  return BY_ID[id].meta.parent || id;
}

function backButton(id) {
  const t = backTarget(id);
  const title = BY_ID[t.id]?.meta.title || '上一页';
  return h('button.btn.btn-sm.back-btn', {
    title: `回到「${title}」`,
    onclick: () => navigate(t.id, { arg: t.arg, back: true }),
  }, `← ${title}`);
}

buildNav('#navMain', MAIN_NAV, '平台');
buildNav('#navAnalysis', ANALYSIS_NAV, '分析');
buildNav('#navSystem', SYSTEM_NAV, '系统');

function fromHash() {
  // `#/settings` 和 `#settings` 是一回事。原来前者被切成 id=''，落到总览（自查 §6 #21）
  const raw = location.hash.slice(1).replace(/^\/+/, '');
  const [id, ...rest] = (raw || 'overview').split('/');
  return { id: id || 'overview', arg: rest.join('/') || null };
}

window.addEventListener('hashchange', () => {
  // 刚才 navigate() 自己写的那个 hash —— 页面已经渲染过了，不再来一遍
  if (selfWritten !== null && location.hash.slice(1) === selfWritten) {
    selfWritten = null;
    return;
  }
  selfWritten = null;
  const { id, arg } = fromHash();
  navigate(id, { push: false, arg });
});

// ------------------------------------------------------------------ 健康检查
async function checkHealth() {
  const el = $('#healthStatus');
  try {
    const info = await api.health();
    el.className = 'status status-ok small';
    el.textContent = `本地服务正常 · ${info.modules} 个功能模块`;
    $('#versionLabel').textContent = `v${info.version}`;
    if (info.module_errors) {
      el.className = 'status status-warn small';
      el.textContent = `${info.module_errors} 个功能模块加载失败`;
    }
  } catch {
    el.className = 'status status-danger small';
    el.textContent = '连不上本地服务';
  }
}

checkHealth();
setInterval(checkHealth, 30000);

// ------------------------------------------------------------------ AI 抽屉
initDrawer({ nav: navigate, currentPage: () => current });

const initial = fromHash();
navigate(initial.id, { push: false, arg: initial.arg });
