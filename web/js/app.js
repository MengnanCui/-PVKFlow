// 应用外壳：导航、主题、健康检查。

import { api } from './api.js';
import { h, mount, $, toast } from './ui.js';

import * as overview from './pages/overview.js';
import * as process_ from './pages/process.js';
import * as sample from './pages/sample.js';
import * as batchPage from './pages/batch.js';
import * as history from './pages/history.js';
import * as storage from './pages/storage.js';
import * as relation from './pages/relation.js';
import * as settings from './pages/settings.js';
import { initDrawer } from './components/ai-drawer.js';

const PAGES = [overview, process_, sample, batchPage, history, storage, relation, settings];
const BY_ID = Object.fromEntries(PAGES.map((p) => [p.meta.id, p]));

// sample 不出现在侧栏 —— 它是从数据处理页下钻进去的。
//
// 三组是有先后的：「平台」是数据的正向流程（处理 → 存储 → 构效关系），
// 「分析」是回头看已经跑过的东西。对比历史属于后者，跟前面三个不是一个层级，
// 所以单独成组排在后面，而不是塞进主线里当第三项。
const MAIN_NAV = ['overview', 'process', 'storage', 'relation'];
const ANALYSIS_NAV = ['history'];
const SYSTEM_NAV = ['settings'];

const viewport = $('#viewport');
const mounted = new Map();
let current = null;

// ------------------------------------------------------------------ 主题
const THEME_KEY = 'hte.theme';
function applyTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* 隐私模式下会抛，忽略 */ }
}
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* 同上 */ }
  applyTheme(saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
})();
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
function navigate(id, { push = true, arg = null } = {}) {
  const page = BY_ID[id];
  if (!page) return navigate('overview');

  current = id;
  // 下钻页面把父级导航项保持高亮，用户才不会觉得自己"掉出去"了
  const highlight = page.meta.parent || id;
  document.querySelectorAll('.nav-item').forEach((b) => {
    if (b.dataset.page === highlight) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });

  $('#pageTitle').textContent = page.meta.title;
  $('#pageDesc').textContent = page.meta.desc || '';
  mount($('#pageActions'), ...(page.actions?.(navigate, arg) || []));

  let host = mounted.get(id);
  if (!host) {
    host = h(`section.page#page-${id}`);
    viewport.appendChild(host);
    mounted.set(id, host);
  }
  for (const [pid, node] of mounted) node.classList.toggle('is-active', pid === id);

  // 每次进入都重新取数 —— 数据会变，缓存一份旧的更糟
  page.view(host, { nav: navigate, arg });

  const target = arg ? `${id}/${arg}` : id;
  if (push && location.hash.slice(1) !== target) location.hash = target;
  document.title = `${page.meta.title} · HTE Studio`;
  viewport.scrollTop = 0;
}

buildNav('#navMain', MAIN_NAV, '平台');
buildNav('#navAnalysis', ANALYSIS_NAV, '分析');
buildNav('#navSystem', SYSTEM_NAV, '系统');

function fromHash() {
  const [id, ...rest] = (location.hash.slice(1) || 'overview').split('/');
  return { id: id || 'overview', arg: rest.join('/') || null };
}

window.addEventListener('hashchange', () => {
  const { id, arg } = fromHash();
  navigate(id, { push: false, arg });
});
window.addEventListener('hte:nav', (e) => {
  const d = e.detail;
  if (typeof d === 'string') navigate(d);
  else navigate(d.page, { arg: d.arg });
});

// ------------------------------------------------------------------ 健康检查
async function checkHealth() {
  const el = $('#healthStatus');
  try {
    const info = await api.health();
    el.className = 'status status-ok small';
    el.textContent = `本地服务正常 · ${info.skills} 个 Skill`;
    $('#versionLabel').textContent = `v${info.version}`;
    if (info.skill_errors) {
      el.className = 'status status-warn small';
      el.textContent = `${info.skill_errors} 个 Skill 加载失败`;
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
