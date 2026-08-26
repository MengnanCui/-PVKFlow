// 应用外壳：导航、主题、健康检查。

import { api } from './api.js';
import { h, mount, $, toast } from './ui.js';

import * as overview from './pages/overview.js';
import * as process_ from './pages/process.js';
import * as storage from './pages/storage.js';
import * as relation from './pages/relation.js';
import * as settings from './pages/settings.js';

const PAGES = [overview, process_, storage, relation, settings];
const BY_ID = Object.fromEntries(PAGES.map((p) => [p.meta.id, p]));

const MAIN_NAV = ['overview', 'process', 'storage', 'relation'];
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

function navigate(id, { push = true } = {}) {
  const page = BY_ID[id];
  if (!page) return navigate('overview');

  current = id;
  document.querySelectorAll('.nav-item').forEach((b) => {
    if (b.dataset.page === id) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });

  $('#pageTitle').textContent = page.meta.title;
  $('#pageDesc').textContent = page.meta.desc || '';
  mount($('#pageActions'), ...(page.actions?.(navigate) || []));

  let host = mounted.get(id);
  if (!host) {
    host = h(`section.page#page-${id}`);
    viewport.appendChild(host);
    mounted.set(id, host);
  }
  for (const [pid, node] of mounted) node.classList.toggle('is-active', pid === id);

  // 每次进入都重新取数 —— 数据会变，缓存一份旧的更糟
  page.view(host, { nav: navigate });

  if (push && location.hash.slice(1) !== id) location.hash = id;
  document.title = `${page.meta.title} · HTE Studio`;
  viewport.scrollTop = 0;
}

buildNav('#navMain', MAIN_NAV, '平台');
buildNav('#navSystem', SYSTEM_NAV, '系统');

window.addEventListener('hashchange', () => navigate(location.hash.slice(1) || 'overview', { push: false }));
window.addEventListener('hte:nav', (e) => navigate(e.detail));

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

navigate(location.hash.slice(1) || 'overview', { push: false });
