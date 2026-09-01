// 数据处理 —— 功能切换 + 分面筛选 + 样品列表 + 批处理入口。
//
// 核心：**一次选择是一个筛选式，不是一串 ID**。左边的分面面板改的是筛选式，
// 中间的列表只是它的一个视图。手点几个之后平台会提议「要不要扩展成规则」。

import { api } from '../api.js';
import { infoDot } from '../components/info.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy,
  fmtBytes, fmtInt, modal,
} from '../ui.js';
import { openImportDialog, bindDropUpload } from '../components/filepicker.js';
import { facetPanel, filterSummary } from '../components/facets.js';
import { virtualList } from '../components/virtual-list.js';
import { setScope } from '../scope.js';

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
  { id: 'qfls', name: 'QFLS', ready: false, desc: '准费米能级分裂' },
];

const PAGE = 200;

const S = {
  fn: 'abs-thickness',
  filter: {},
  facets: null,
  rows: [],          // 已加载的页
  total: 0,
  loading: false,
  checked: new Set(),   // 手工勾选的 sample_id
  anchor: null,         // 上次点选的行号，shift 范围选从这儿起
  sets: [],
  task: null,
  pollTimer: null,
};

let refs = {};

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
  refs = { host, nav: ctx.nav };
  S.checked = new Set();
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
    disabled: !f.ready,
    title: f.ready ? f.desc : '规划中',
    onclick: () => { if (f.ready) { S.fn = f.id; S.filter = {}; view(refs.host, refs); } },
  }, h('span.fn-tab-name', f.name),
     h('span.fn-tab-desc', f.ready ? f.desc : '规划中'))));
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
async function reload() {
  if (!refs.listHost) return;
  S.loading = true;
  drawList();
  try {
    const [page, facets] = await Promise.all([
      api.selectionQuery({ filter: S.filter, limit: PAGE }),
      api.selectionFacets({ filter: S.filter }),
    ]);
    S.rows = page.rows;
    S.total = page.total;
    S.facets = facets;
    S.error = null;
  } catch (err) {
    S.error = err;
  }
  S.loading = false;
  drawFacets();
  drawList();
  publishScope();
}

/** 把「当前在看哪批样品」告诉 AI 抽屉。抽屉不该来读这里的私有 S。 */
function publishScope() {
  setScope({ filter: S.filter, total: S.total,
             checked: [...S.checked], page: 'process' });
}

function setFilter(next) {
  S.filter = next;
  S.checked = new Set();
  reload();
}

async function loadSets() {
  try { S.sets = (await api.listSets()).sets; } catch { S.sets = []; }
  drawFacets();
}

// ------------------------------------------------------------------ 左栏

/**
 * 搜索框**建一次就一直用那一个节点**。
 *
 * 以前它写在 drawFacets() 里，而每次筛选变化都会重建整个左栏 ——
 * 于是你敲完回车，输入框被换成了一个新的，焦点和光标位置一起没了，
 * 想接着改关键词得再点一次。这是打字时最直接的一处「不顺滑」。
 */
function searchBox() {
  if (!refs.search) {
    refs.search = h('input.input', {
      placeholder: '搜索样品名 / 批次…',
      onchange: (e) => {
        const next = { ...S.filter };
        if (e.target.value.trim()) next.q = e.target.value.trim();
        else delete next.q;
        setFilter(next);
      },
    });
  }
  // 值由 S.filter 来定，但**别在用户正打字的时候改它** ——
  // 那会把光标弹到末尾。
  if (document.activeElement !== refs.search) refs.search.value = S.filter.q || '';
  return refs.search;
}

function drawFacets() {
  if (!refs.facetHost) return;
  if (!S.facets) { mount(refs.facetHost, skeletonRows(6)); return; }

  // 光留住节点还不够：mount() 会把它摘下来再插回去，而**移动一个带焦点的元素
  // 会让它失焦**。所以插完再把焦点和选区放回去。
  const box = searchBox();
  const hadFocus = document.activeElement === box;
  const caret = hadFocus ? [box.selectionStart, box.selectionEnd] : null;

  mount(refs.facetHost,
    h('div.row-between',
      h('div.panel-title', '筛选'),
      h('span.small.muted', `${fmtInt(S.facets.total)} 个样品`)),
    h('div.mt-3', box),
    S.sets.length ? savedSets() : null,
    h('div.divider'),
    facetPanel({ facets: S.facets, filter: S.filter, onChange: setFilter }));

  if (hadFocus) {
    box.focus({ preventScroll: true });
    try { box.setSelectionRange(caret[0], caret[1]); } catch { /* 有些类型不支持选区 */ }
  }
}

function savedSets() {
  return h('div.mt-4',
    h('div.facet-head', h('span.facet-title', '样品集')),
    h('div.facet-chips', ...S.sets.map((s) =>
      h('button.chip-toggle', {
        title: s.kind === 'dynamic' ? '动态集：新导入的样品会自动进来'
                                    : '固定集：钉死的快照，不随新数据变',
        onclick: async () => {
          try {
            const resolved = await api.getSet(s.set_id);
            setFilter(resolved.kind === 'pinned'
              ? { ids: resolved.pinned_ids } : resolved.filter);
            toast(`已套用「${s.name}」`, 'ok');
          } catch (err) { toast(err.message, 'err'); }
        },
      },
        h('span.chip-label', s.name),
        h('span.chip-count', fmtInt(s.count)),
        s.kind === 'pinned' ? h('span.xsmall.dim', '钉') : null))));
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
          h('span.xsmall.dim', '点行选中 · 按住拖着刷一片 · shift+点 选中间一整段 · 双击或「打开」进样品',
            infoDot('sample_identity')),
          S.total > all
            ? h('span.xsmall.dim',
                `· 列表载入前 ${fmtInt(all)} 行，「批处理全部」按的是筛选式命中的 ${fmtInt(S.total)} 个`,
                infoDot('filter_expr'))
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
      h('div.panel-body.flush#tableHost')));

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
  refs.vlist = list;
  mount(host, header, list);
}

// ------------------------------------------------------------------ 勾选
//
// 挑样品是这一页的主要动作，所以**整行就是勾选热区**，点哪儿都是选。
// 打开样品要走行尾的「打开」按钮或者双击 —— 之前点行体直接跳走，
// 挑到一半被弹到详情页，回来滚动和勾选全没了。
//
// 三种手势：
//   点         切换这一行，并把锚点挪过来
//   按住拖     从按下那一行刷到当前行，按下时是选就一路选、是取消就一路取消
//   shift+点   锚点到这一行之间**加选**（已经选好的不会被抹掉）
//
// 复选框本身设了 pointer-events: none —— 它只是状态的显示，不是独立的按钮。
// 让它可点会有两套状态在打架：浏览器自己的 checked 和我们的 S.checked。

const drag = { on: false, anchor: null, mode: 'add' };

function selectRange(a, b, mode) {
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) {
    const r = S.rows[i];
    if (!r) continue;
    if (mode === 'add') S.checked.add(r.sample_id);
    else S.checked.delete(r.sample_id);
  }
}

function beginSelect(index, evt) {
  const row = S.rows[index];
  if (!row) return;

  if (evt.shiftKey && S.anchor !== null && S.anchor !== index) {
    selectRange(S.anchor, index, 'add');
    syncSelection();
    return;                       // shift 是一次性的范围选，不进入拖拽
  }

  // 按下时这一行是「将要被选中」还是「将要被取消」，决定整段刷的方向。
  // 从一个已选的行开始拖 = 擦掉一片，这跟表格软件里的行为一致。
  drag.on = true;
  drag.anchor = index;
  pointer.x = evt.clientX;
  pointer.y = evt.clientY;
  autoScroll();
  drag.mode = S.checked.has(row.sample_id) ? 'remove' : 'add';
  S.anchor = index;
  selectRange(index, index, drag.mode);
  syncSelection();
}

function extendSelect(index) {
  if (!drag.on) return;
  selectRange(drag.anchor, index, drag.mode);
  syncSelection();
}

// 松手可能发生在列表外面（拖出了窗口），所以监听在 window 上，只装一次
window.addEventListener('pointerup', endDrag);
window.addEventListener('pointercancel', endDrag);
window.addEventListener('pointermove', (e) => {
  if (!drag.on) return;
  pointer.x = e.clientX;
  pointer.y = e.clientY;
});

function endDrag() {
  drag.on = false;
  if (raf) { cancelAnimationFrame(raf); raf = null; }
}

// 拖到列表上下边缘就自动滚 —— 没有这个的话一次只能刷视口里那十几行，
// 四十个样品想全选还是得点点点。
const pointer = { x: 0, y: 0 };
const EDGE = 34;          // 离边缘多近开始滚
let raf = null;

function autoScroll() {
  raf = requestAnimationFrame(function step() {
    if (!drag.on) { raf = null; return; }
    const list = refs.vlist;
    if (list) {
      const r = list.getBoundingClientRect();
      let dy = 0;
      if (pointer.y < r.top + EDGE) dy = -Math.ceil((r.top + EDGE - pointer.y) / 2);
      else if (pointer.y > r.bottom - EDGE) dy = Math.ceil((pointer.y - (r.bottom - EDGE)) / 2);
      if (dy) list.scrollTop += dy;

      // 内容在不动的指针下面滚过去时 pointerenter 不会触发，
      // 所以每一帧都自己问一次「现在指针底下是哪一行」。
      const el = document.elementFromPoint(pointer.x, pointer.y);
      const row = el?.closest?.('.vrow[data-index]');
      if (row) extendSelect(Number(row.dataset.index));
    }
    raf = requestAnimationFrame(step);
  });
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
    'aria-selected': String(checked),
    dataset: { index: String(index) },
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

// 「选择即示例」那个建议条删掉了：它挂在列表上方向下弹出，一出现就把
// 下面的样品行推走，正在挑样品的时候根本点不中。后端 /api/selection/suggest
// 和 selection.suggest_expansion 都留着 —— 以后想要，换个不挡路的位置接回来。

// ------------------------------------------------------------------ 批处理
// 对比的默认配方。和单样品页的默认窗口一致 ——
// 775 避开吸收边、1120 是光谱仪上限。进去之后在对比页上随便改。
const DEFAULT_RECIPE = {
  band_min: 775, band_max: 1120,
  integral_min: 800, integral_max: 950,
  slope_center: 950, slope_half_width: 10,
};

/** 选中几个 → 直接开跑 → 直接落到对比页（页面上自己显示进度）。 */
async function startCompare(btn) {
  const useChecked = S.checked.size > 0;
  const filter = useChecked ? { ids: [...S.checked] } : S.filter;

  busy(btn, true);
  try {
    const pv = await api.batchPreview(filter);
    if (!pv.with_matrix) {
      toast('选中的样品里没有光谱矩阵，没法对比', 'err');
      return;
    }
    if (pv.without_matrix) {
      // 跳过了一些样品要说出来，别让人以为全都比了
      toast(`${pv.without_matrix} 个样品没有光谱矩阵，这次跳过`, 'warn');
    }
    const stamp = new Date().toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    const r = await api.batchRun({
      filter, recipe: DEFAULT_RECIPE,
      title: `${pv.with_matrix} 个样品 · ${stamp}`,
    });
    // 传 task_id：这一刻还没有 parent_run_id，对比页会自己等
    refs.nav('batch', { arg: r.task.task_id });
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    busy(btn, false);
  }
}

async function batchDialog(recipeOverride = null) {
  const useChecked = S.checked.size > 0;
  const filter = useChecked ? { ids: [...S.checked] } : S.filter;

  let pv;
  try {
    pv = await api.batchPreview(filter);
  } catch (err) { toast(err.message, 'err'); return; }

  if (!pv.with_matrix) {
    toast('选中的样品里没有光谱矩阵', 'err');
    return;
  }

  // 膜厚窗口默认 775–1120：775 避开吸收边，1120 是光谱仪上限。
  // AI 卡片带来的配方只是**预填**，对话框照样要你确认才开跑。
  const recipe = { integral_min: 800, integral_max: 950,
                   slope_center: 950, slope_half_width: 10,
                   band_min: 775, band_max: 1120,
                   ...(recipeOverride || {}) };
  const stamp = new Date().toLocaleString('zh-CN', { month: '2-digit', day: '2-digit',
                                                    hour: '2-digit', minute: '2-digit' });
  let title = `${pv.with_matrix} 个样品 · ${stamp}`;
  const nameInput = h('input.input', {
    type: 'text', value: title, placeholder: '这次对比叫什么',
    oninput: (e) => { title = e.target.value; },
  });
  const num = (key, label, unit) => h('div.field',
    h('label.field-label', label, unit ? h('span.unit', ` (${unit})`) : null),
    h('input.input', { type: 'number', value: recipe[key], step: 'any',
      oninput: (e) => { recipe[key] = Number(e.target.value); } }));

  const m = modal({
    title: `批处理 ${fmtInt(pv.with_matrix)} 个样品`,
    width: '620px',
    body: h('div',
      h('p.small.muted',
        `命中 ${fmtInt(pv.total)} 个样品，其中 ${fmtInt(pv.with_matrix)} 个有光谱矩阵`,
        pv.without_matrix ? `，${fmtInt(pv.without_matrix)} 个会被跳过` : '', '。'),
      h('div.field.mt-3',
        h('label.field-label', '名称',
          h('span.unit', ' （会出现在对比历史里，方便以后找回来）')),
        nameInput),
      h('div.notice.mt-3',
        h('div.grow',
          h('div.small', '这套参数就是单样品页上的那套 —— '
            + '在单个样品上调好之后回到这里，填一样的值即可。')),
      ),
      h('div.recipe-grid.mt-4',
        num('band_min', '膜厚窗口起', 'nm'), num('band_max', '膜厚窗口止', 'nm'),
        num('integral_min', '积分波段起', 'nm'), num('integral_max', '积分波段止', 'nm'),
        num('slope_center', '斜率波长', 'nm'), num('slope_half_width', '斜率窗口半宽', 'nm'))),
    foot: [
      h('span.small.muted.grow', '跑在后台，可以离开这个页面'),
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('button.btn.btn-primary', {
        onclick: async (e) => {
          busy(e.target, true);
          try {
            const r = await api.batchRun({ filter, recipe,
              title: title.trim() || `${pv.with_matrix} 个样品` });
            m.close();
            S.task = r.task;
            drawList();
            startPolling(r.task.task_id);
          } catch (err) {
            busy(e.target, false);
            toast(err.message, 'err', 6000);
          }
        },
      }, '开始'),
    ],
  });
}

function progressBlock() {
  const t = S.task;
  const pct = t.percent ?? 0;
  return h('div.progress-card.mb-3',
    h('div.row-between',
      h('div',
        h('div.small.strong', t.title || '批处理'),
        h('div.xsmall.dim.mt-2', t.message || statusText(t.status))),
      h('div.row.gap-2',
        t.done
          ? h('button.btn.btn-sm', {
              onclick: () => { S.task = null; drawList(); } }, '收起')
          : h('button.btn.btn-sm', {
              onclick: async (e) => {
                busy(e.target, true);
                try { await api.cancelTask(t.task_id); } catch (err) { toast(err.message, 'err'); }
                busy(e.target, false);
              } }, '取消'),
        t.done && t.status === 'ok' && t.result?.parent_run_id
          ? h('button.btn.btn-primary.btn-sm', {
              onclick: () => refs.nav('batch', { arg: t.result.parent_run_id }),
            }, '看结果')
          : null)),
    h('div.progress-bar', h('i', { style: { width: `${pct}%` } })),
    h('div.row-between',
      h('span.xsmall.dim', `${fmtInt(t.progress)} / ${fmtInt(t.total)}`),
      h('span.xsmall.dim',
        t.n_failed ? `${fmtInt(t.n_ok)} 成功 · ${fmtInt(t.n_failed)} 失败`
                   : `${fmtInt(t.n_ok)} 成功`)));
}

const statusText = (s) => ({ queued: '排队中', running: '进行中', ok: '完成',
                             failed: '失败', cancelled: '已取消' }[s] || s);

function startPolling(taskId) {
  stopPolling();
  S.pollTimer = setInterval(async () => {
    try {
      const t = await api.getTask(taskId);
      S.task = t;
      drawList();
      if (t.done) {
        stopPolling();
        if (t.status === 'ok') {
          toast(`处理完成：${t.n_ok} 成功` + (t.n_failed ? `，${t.n_failed} 失败` : ''), 'ok');
          // 跑完直接落到对比页。以前还要再点一下「看结果」，而那个按钮
          // 一刷新就没了 —— 「比完就没了、找不到在哪儿」说的就是这个。
          // 现在它同时也进了「对比历史」，随时点得回来。
          if (t.result?.parent_run_id) {
            refs.nav('batch', { arg: t.result.parent_run_id });
            return;
          }
          reload();
        } else if (t.status === 'failed') {
          toast(t.error?.split('\n')[0] || '批处理失败', 'err', 8000);
        }
      }
    } catch { stopPolling(); }
  }, 700);
}

function stopPolling() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
}

// ------------------------------------------------------------------ 样品集
function saveSetDialog() {
  const useChecked = S.checked.size > 0;
  const nameInput = h('input.input', { placeholder: '比如 B20 全批' });
  let kind = useChecked ? 'pinned' : 'dynamic';

  const kindRow = (value, title, desc) => h('label.kind-option',
    h('input', { type: 'radio', name: 'setkind', value, checked: kind === value,
                 onchange: () => { kind = value; } }),
    h('div', h('div.small.strong', title), h('div.xsmall.dim', desc)));

  const m = modal({
    title: '存为样品集',
    width: '560px',
    body: h('div',
      h('div.field', h('label.field-label', '名字'), nameInput),
      h('div.col.gap-2.mt-4',
        kindRow('dynamic', '动态集（存筛选式）',
          '以后新导入的、符合这个筛选式的样品会自动进来。适合「B20 全批」这种。'),
        kindRow('pinned', '固定集（存快照）',
          `钉死当前这 ${fmtInt(useChecked ? S.checked.size : S.facets?.total || 0)} 个，`
          + '不随新数据变。论文里的图要用这种。')),
      h('p.xsmall.dim.mt-3',
        '两者语义完全不同：论文图对应的样品集如果是动态的，重跑一次数字就变了。')),
    foot: [
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('button.btn.btn-primary', {
        onclick: async (e) => {
          busy(e.target, true);
          try {
            const body = { name: nameInput.value, kind };
            if (kind === 'pinned') {
              body.sample_ids = useChecked ? [...S.checked] : undefined;
              if (!body.sample_ids) body.filter = S.filter;
            } else {
              body.filter = useChecked ? { ids: [...S.checked] } : S.filter;
            }
            await api.createSet(body);
            m.close();
            toast('样品集已保存', 'ok');
            loadSets();
          } catch (err) {
            busy(e.target, false);
            toast(err.message, 'err', 6000);
          }
        },
      }, '保存'),
    ],
  });
}

function openImport() {
  openImportDialog({ onDone: () => reload() });
}
