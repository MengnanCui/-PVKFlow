// 数据处理 —— 功能切换 + 分面筛选 + 样品列表 + 批处理入口。
//
// 核心：**一次选择是一个筛选式，不是一串 ID**。左边的分面面板改的是筛选式，
// 中间的列表只是它的一个视图。手点几个之后平台会提议「要不要扩展成规则」。
//
// 原来一个文件七百多行，2026-11 按职责拆开（tools/split_page.py），行为不变：
//   process-state.js   页面状态 S / refs，和 hooks（见下）
//   process-filter.js  搜索框、分面、保存的样品集
//   process-select.js  在列表上拖选
//   process-run.js     交给批处理：确认、进度、跳到对比页
// 拆出去的三个要回头调本文件的 reload / drawList / syncSelection —— 直接 import 会成环
// （离线烤制要求依赖无环），所以本文件加载时把它们登记到 hooks 上，那三个经 hooks 调。

import { api } from '../api.js';
import { infoDot } from '../components/info.js';
import { h, mount, toast, empty, skeletonRows, errorBox, fmtBytes, fmtInt, latest } from '../ui.js';
import { openImportDialog, bindDropUpload } from '../components/filepicker.js';
import { filterSummary } from '../components/facets.js';
import { virtualList } from '../components/virtual-list.js';
import { drawFacets, loadSets, publishScope, saveSetDialog, setFilter } from './process-filter.js';
import { batchDialog, progressBlock, startCompare, stopPolling } from './process-run.js';
import { beginSelect, extendSelect, rowKey } from './process-select.js';
import { S, hooks, refs, resetRefs } from './process-state.js';

// 拆出去的文件经 hooks 回调这几个（见 process-state.js）
Object.assign(hooks, { drawList, reload, syncSelection });

export const meta = {
  id: 'process',
  title: '数据处理',
  desc: '选一个功能，筛出一批样品',
};

const FUNCTIONS = [
  { id: 'abs-thickness', name: '吸收光谱 & 膜厚', ready: true,
    desc: '原位吸收光谱矩阵：强度热力图、干涉条纹、光学膜厚、谱斜率与波段积分' },
  { id: 'pl-pl', name: '荧光 & 荧光', ready: false, desc: '双通道荧光关联' },
  { id: 'pl-scatter', name: '荧光 & 散射', ready: false, desc: '荧光与散射同步采集' },
  // QFLS 是一条独立的流水线（实验 / 批次 / slot / point 的目录，不走样品库），在「分析 → QFLS 成像」
  { id: 'qfls', name: 'QFLS', ready: false, goto: 'qfls', desc: '在「QFLS 成像」页处理（独立流水线）' },
];

const PAGE = 200;

export function actions(nav) {
  return [h('button.btn.btn-primary', { onclick: () => openImport() }, '导入数据')];
}

// AI 抽屉里的筛选式卡片点「应用」时走这条。**只是把筛选式填进来**，
// 跑不跑仍然要你自己点 —— 模型能写的操作都得先变成你看得见、能改、能拒绝的东西。
let pendingFromAI = null;
window.addEventListener('hte:apply-filter', (e) => {
  pendingFromAI = e.detail;
  if (refs.listHost) consumeAIFilter();
});

function consumeAIFilter() {
  const p = pendingFromAI;
  if (!p) return;
  pendingFromAI = null;
  setFilter(p.filter || {});
  if (p.openBatch) {
    // 等这一轮取数回来再开对话框，否则命中数还是上一次的
    reload().then(() => batchDialog(p.recipe || null));
  }
}

export async function view(host, ctx) {
  resetRefs({ host, nav: ctx.nav, ctx, alive: ctx.alive || (() => true) });
  // 勾选不在这里清：从样品页回来还要在（原来每次进页面都清空，C070）。改筛选时 process-filter 会清，
  // 切功能时下面清
  stopPolling();

  const shell = h('div');
  mount(host, h('div.section', functionTabs()), shell);
  refs.shell = shell;
  bindDropUpload(host, () => reload());

  drawShell();
  await Promise.all([reload(), loadSets()]);
  consumeAIFilter();
}

function functionTabs() {
  return h('div.fn-tabs', ...FUNCTIONS.map((f) => h('button.fn-tab', {
    'aria-selected': String(S.fn === f.id),
    disabled: !f.ready && !f.goto,
    title: f.ready || f.goto ? f.desc : '规划中',
    onclick: () => {
      if (f.goto) { refs.nav(f.goto); return; }
      if (f.ready) { S.fn = f.id; S.filter = {}; S.checked = new Set(); view(refs.host, refs.ctx); }
    },
  }, h('span.fn-tab-name', f.name, f.goto ? ' →' : ''),
     h('span.fn-tab-desc', f.ready || f.goto ? f.desc : '规划中'))));
}

function drawShell() {
  const fn = FUNCTIONS.find((f) => f.id === S.fn);
  if (!fn.ready) {
    mount(refs.shell, h('div.panel', empty(`${fn.name} 还没有开始做`)));
    return;
  }
  const facetHost = h('aside#facetHost.panel.panel-body');
  const listHost = h('div#listHost');
  mount(refs.shell, h('div.select-layout', facetHost, h('div.min0', listHost)));
  refs.facetHost = facetHost;
  refs.listHost = listHost;
}

// ------------------------------------------------------------------ 取数
// 快速连点筛选时，先发的请求可能后回来 —— 列表会停在第一下的结果上、
// 计数和 AI 抽屉拿到的范围也跟着错（自查 §6 #18）。只认最后一次。
const reloadTicket = latest();

async function reload() {
  if (!refs.listHost) return;
  const fresh = reloadTicket();
  S.loading = true;
  drawList();
  try {
    const [page, facets] = await Promise.all([
      api.selectionQuery({ filter: S.filter, limit: PAGE }),
      api.selectionFacets({ filter: S.filter }),
    ]);
    if (!fresh()) return;
    S.rows = page.rows;
    S.total = page.total;
    S.facets = facets;
    S.error = null;
  } catch (err) {
    if (!fresh()) return;
    S.error = err;
  }
  S.loading = false;
  drawFacets();
  drawList();
  publishScope();
}

// ------------------------------------------------------------------ 右栏
function drawList() {
  if (!refs.listHost) return;
  if (S.error) { mount(refs.listHost, errorBox(S.error, reload)); return; }
  if (S.loading && !S.rows.length) { mount(refs.listHost, skeletonRows(8)); return; }

  const selectedCount = S.checked.size;
  const all = S.rows.length;

  mount(refs.listHost,
    h('div.row-between.mb-3',
      h('div.filter-line', filterSummary(S.filter, setFilter)),
      h('div.row.gap-2',
        h('button.btn.btn-sm', { onclick: () => openImport() }, '导入数据'),
        h('button.btn.btn-sm', {
          disabled: !S.facets?.total,
          onclick: () => saveSetDialog(),
        }, '存为样品集'))),

    S.task ? progressBlock() : null,

    h('div.panel',
      h('div.panel-head',
        h('div.row.gap-3',
          h('label.check',
            h('input#selectAllBox', {
              type: 'checkbox',
              checked: selectedCount > 0 && selectedCount === all,
              indeterminate: selectedCount > 0 && selectedCount < all,
              onchange: (e) => {
                S.checked = e.target.checked
                  ? new Set(S.rows.map((r) => r.sample_id)) : new Set();
                S.anchor = null;
                syncSelection();
              },
            }),
            h('span.small#selectCount',
              selectedCount ? `已选 ${fmtInt(selectedCount)}` : '全选本页')),
          h('span.xsmall.dim', '点行选中 · 按住拖着刷一片 · shift+点 选中间一整段 · 双击或「打开」进样品'
            + ' · 键盘：上下键移动、空格选、回车打开',
            infoDot('sample_identity')),
          S.total > all
            ? h('span.xsmall.dim',
                `· 列表载入前 ${fmtInt(all)} 行，「批处理全部」按的是筛选式命中的 ${fmtInt(S.total)} 个`,
                // 「我到底选中了什么」是这一处最常见的问题，而答案就是
                // 当前这个筛选式和它命中的数
                infoDot('filter_expr', {
                  spot: () => ({
                    title: '当前的筛选式',
                    rows: {
                      筛选式: Object.keys(S.filter).length
                        ? JSON.stringify(S.filter) : '（无筛选，全库）',
                      命中样品数: fmtInt(S.total),
                      列表已载入: `${fmtInt(all)} 行`,
                      手工勾选: selectedCount ? `${fmtInt(selectedCount)} 个` : '（没有）',
                    },
                    note: '手工勾选优先于筛选式：勾了就只处理勾中的那些。',
                    scope: { filter: S.filter },
                  }),
                }))
            : null),
        h('div.row.gap-2',
          h('button.btn.btn-sm#clearSelBtn', {
            disabled: !selectedCount,
            onclick: () => {
              S.checked = new Set(); S.anchor = null;
              syncSelection();
            },
          }, '取消选择'),
          h('button.btn.btn-primary.btn-sm#runBatchBtn', {
            disabled: !S.facets?.total,
            // 直接跑，用默认配方，直接落到对比页。
            // 原来先弹一个填六个数的对话框 —— 可那时候你还没看到任何结果，
            // 六个数该填什么根本无从判断。参数现在摆在对比页上，
            // 先看到东西再改，改完重跑就是新的一次对比。
            onclick: (e) => startCompare(e.target),
          }, batchLabel()))),
      h('div.panel-body.flush.vlist-container#tableHost')));

  drawTable();
}

function batchLabel() {
  if (S.checked.size) return `批处理选中的 ${fmtInt(S.checked.size)} 个`;
  return `批处理全部 ${fmtInt(S.facets?.total || 0)} 个`;
}

function drawTable() {
  const host = refs.listHost.querySelector('#tableHost');
  if (!host) return;
  if (!S.rows.length) {
    mount(host, empty(
      Object.keys(S.filter).length ? '这个筛选式没有命中样品' : '还没有导入数据',
      Object.keys(S.filter).length
        ? h('button.btn.btn-sm', { onclick: () => setFilter({}) }, '清除筛选')
        : h('button.btn.btn-sm', { onclick: () => openImport() }, '导入数据')));
    return;
  }

  const header = h('div.vrow.vrow-head',
    h('span'), h('span', '样品'), h('span', '样品号'), h('span', '测量时间'),
    h('span', '光谱矩阵'),
    h('span', { style: { textAlign: 'right' } }, '大小'), h('span'));

  // 上千行全塞进 DOM 会卡死浏览器，只渲染视口里那几十行
  const list = virtualList({
    height: Math.min(560, Math.max(220, S.rows.length * 46 + 8)),
    rowHeight: 46,
    count: S.rows.length,
    renderRow: (i) => renderRow(S.rows[i], i),
  });
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-multiselectable', 'true');
  list.setAttribute('aria-label', '样品列表');
  refs.vlist = list;
  mount(host, header, list);
}

/**
 * 只更新受影响的地方，不重建整个列表。
 *
 * 以前每勾一下都调 drawList()，虚拟列表整个重建 —— 滚动位置被打回顶部。
 * 选到第 80 行的时候这个行为是真的难用。
 */
function syncSelection() {
  const host = refs.listHost;
  if (!host) return;

  for (const el of host.querySelectorAll('.vrow[data-index]')) {
    const i = Number(el.dataset.index);
    const on = S.checked.has(S.rows[i]?.sample_id);
    el.setAttribute('aria-selected', String(on));
    const box = el.querySelector('input[type=checkbox]');
    if (box) box.checked = on;
  }

  const n = S.checked.size;
  const all = S.rows.length;
  const head = host.querySelector('#selectAllBox');
  if (head) {
    head.checked = n > 0 && n === all;
    head.indeterminate = n > 0 && n < all;
  }
  const label = host.querySelector('#selectCount');
  if (label) label.textContent = n ? `已选 ${fmtInt(n)}` : '全选本页';
  const clearBtn = host.querySelector('#clearSelBtn');
  if (clearBtn) clearBtn.disabled = !n;
  const runBtn = host.querySelector('#runBatchBtn');
  if (runBtn) runBtn.textContent = batchLabel();

  publishScope();
}

function renderRow(r, index) {
  if (!r) return null;
  const checked = S.checked.has(r.sample_id);
  const open = () => {
    if (!r.matrix_id) { toast('这个样品没有光谱矩阵', 'err'); return; }
    refs.nav('sample', { arg: r.matrix_id });
  };
  return h('div.vrow.is-pickable', {
    // 键盘也能选（自查 §6 #30）：Tab 进来，上下键移动，空格勾选，Shift+空格范围选，回车打开
    role: 'option',
    tabindex: '0',
    'aria-selected': String(checked),
    dataset: { index: String(index) },
    onkeydown: (e) => rowKey(e, index, open),
    onpointerdown: (e) => {
      if (e.button !== 0) return;               // 右键留给浏览器菜单
      if (e.target.closest('button')) return;   // 「打开」按钮走自己的路
      e.preventDefault();                       // 拖的时候别让浏览器去选文字
      beginSelect(index, e);
    },
    // 拖到这一行上就把这一行也刷进去。pointerenter 不冒泡，正好一行一次。
    onpointerenter: () => extendSelect(index),
    ondblclick: (e) => { if (!e.target.closest('button')) open(); },
  },
    // 只读的状态显示：真正的开关是整行。让它自己可点的话，浏览器的 checked
    // 和我们的 S.checked 会各说各话（这正是「选中了却不显示打勾」的成因）。
    h('input', { type: 'checkbox', checked, tabIndex: -1, 'aria-hidden': 'true' }),
    h('div.min0',
      h('div.name.truncate', r.name),
      r.n_results ? h('div.xsmall.dim', `${fmtInt(r.n_results)} 条结果`) : null),
    h('span.small.muted', r.batch || '—'),
    // 时间现在是三个筛选维度之一 —— 按它筛，就该看得见它
    h('span.small.muted.nowrap',
      r.measured_at ? String(r.measured_at).slice(0, 16).replace('T', ' ') : '—'),
    h('div.min0',
      r.matrix_name
        ? h('div.truncate.small.muted', { title: r.matrix_name }, r.matrix_name)
        : h('span.xsmall.dim', '无矩阵')),
    h('span.small.muted', { style: { textAlign: 'right' } },
      r.matrix_size ? fmtBytes(r.matrix_size) : '—'),
    h('div', { style: { textAlign: 'right' } },
      r.matrix_id
        ? h('button.btn.btn-sm', { onclick: (e) => { e.stopPropagation(); open(); } }, '打开')
        : null));
}

function openImport() {
  openImportDialog({ onDone: () => reload() });
}
