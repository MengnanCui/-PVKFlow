// 数据处理 —— 功能切换 + 分面筛选 + 样品列表 + 批处理入口。
//
// 核心：**一次选择是一个筛选式，不是一串 ID**。左边的分面面板改的是筛选式，
// 中间的列表只是它的一个视图。手点几个之后平台会提议「要不要扩展成规则」。

import { api } from '../api.js';
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
  suggestions: [],
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
  S.suggestions = [];
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
  S.suggestions = [];
  reload();
}

async function loadSets() {
  try { S.sets = (await api.listSets()).sets; } catch { S.sets = []; }
  drawFacets();
}

// ------------------------------------------------------------------ 左栏
function drawFacets() {
  if (!refs.facetHost) return;
  if (!S.facets) { mount(refs.facetHost, skeletonRows(6)); return; }
  mount(refs.facetHost,
    h('div.row-between',
      h('div.panel-title', '筛选'),
      h('span.small.muted', `${fmtInt(S.facets.total)} 个样品`)),
    h('div.mt-3',
      h('input.input', {
        placeholder: '搜索样品名 / 批次…', value: S.filter.q || '',
        onchange: (e) => {
          const next = { ...S.filter };
          if (e.target.value.trim()) next.q = e.target.value.trim();
          else delete next.q;
          setFilter(next);
        },
      })),
    S.sets.length ? savedSets() : null,
    h('div.divider'),
    facetPanel({ facets: S.facets, filter: S.filter, onChange: setFilter }));
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

    h('div#suggestHost'),
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
                refreshSuggestions();
              },
            }),
            h('span.small#selectCount',
              selectedCount ? `已选 ${fmtInt(selectedCount)}` : '全选本页')),
          h('span.xsmall.dim', 'shift+点 选中间一整段'),
          S.total > all
            ? h('span.xsmall.dim',
                `· 列表载入前 ${fmtInt(all)} 行，「批处理全部」按的是筛选式命中的 ${fmtInt(S.total)} 个`)
            : null),
        h('div.row.gap-2',
          h('button.btn.btn-sm#clearSelBtn', {
            disabled: !selectedCount,
            onclick: () => {
              S.checked = new Set(); S.anchor = null; S.suggestions = [];
              syncSelection(); drawSuggestions();
            },
          }, '取消选择'),
          h('button.btn.btn-primary.btn-sm#runBatchBtn', {
            disabled: !S.facets?.total,
            onclick: () => batchDialog(),
          }, batchLabel()))),
      h('div.panel-body.flush#tableHost')));

  drawTable();
  drawSuggestions();
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

/**
 * 勾选一行。三种手势：
 *   普通    单独切换，并把锚点挪到这一行
 *   shift   锚点到这一行之间**全选**（按列表当前顺序）
 *   ctrl/⌘  切换单个，不动锚点
 *
 * 「从 xx 到 xx」用鼠标就能划出来，不用先去数行号。
 */
function toggleRow(index, evt) {
  const row = S.rows[index];
  if (!row) return;

  if (evt?.shiftKey && S.anchor !== null && S.anchor !== index) {
    const [a, b] = S.anchor < index ? [S.anchor, index] : [index, S.anchor];
    // shift 是「加选这一段」，不是「只留这一段」—— 已经选好的不该被抹掉
    for (let i = a; i <= b; i++) S.checked.add(S.rows[i].sample_id);
  } else if (evt?.metaKey || evt?.ctrlKey) {
    if (S.checked.has(row.sample_id)) S.checked.delete(row.sample_id);
    else S.checked.add(row.sample_id);
  } else {
    if (S.checked.has(row.sample_id)) S.checked.delete(row.sample_id);
    else S.checked.add(row.sample_id);
    S.anchor = index;
  }

  syncSelection();
  refreshSuggestions();
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
  return h('div.vrow', {
    'aria-selected': String(checked),
    dataset: { index: String(index) },
    onclick: (e) => {
      if (e.target.closest('button')) return;
      // 带修饰键点行 = 选，不是打开。不然 shift 划一片会开一堆页面。
      if (e.shiftKey || e.metaKey || e.ctrlKey) {
        e.preventDefault();
        toggleRow(index, e);
        return;
      }
      if (!e.target.closest('input')) open();
    },
  },
    h('input', {
      type: 'checkbox', checked,
      onclick: (e) => { e.stopPropagation(); e.preventDefault(); toggleRow(index, e); },
    }),
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

// ------------------------------------------------------------------ 选择即示例
let suggestTimer = null;
function refreshSuggestions() {
  clearTimeout(suggestTimer);
  if (S.checked.size < 2) { S.suggestions = []; drawSuggestions(); return; }
  suggestTimer = setTimeout(async () => {
    try {
      const r = await api.suggestExpansion([...S.checked], S.filter);
      S.suggestions = r.suggestions;
      drawSuggestions();          // 只换这一块，别把列表和滚动位置一起重置
    } catch { /* 提议失败不该打断操作 */ }
  }, 220);
}

function drawSuggestions() {
  const host = refs.listHost?.querySelector('#suggestHost');
  if (!host) return;
  if (!S.suggestions.length) { clear(host); return; }
  mount(host, suggestionBlock());
}

function suggestionBlock() {
  return h('div.mb-3',
    h('div.small.muted.mb-2',
      `选中的 ${fmtInt(S.checked.size)} 个有共同点 —— 要不要扩展成一条规则？`),
    h('div.suggestions', ...S.suggestions.map((s) =>
      h('button.suggestion', {
        onclick: () => { toast(`已扩展到 ${fmtInt(s.count)} 个样品`, 'ok'); setFilter(s.filter); },
      },
        h('div.min0',
          h('div.small.strong', s.label),
          h('div.suggestion-why', s.why)),
        h('span.suggestion-add', `+${fmtInt(s.adds)}`)))));
}

// ------------------------------------------------------------------ 批处理
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
