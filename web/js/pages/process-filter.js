// 数据处理页：筛选 —— 搜索框、分面、保存的样品集。
// 筛选一变就重新查询（经 hooks.reload，见 process-state.js）。

import { api } from '../api.js';
import { h, mount, toast, skeletonRows, busy, fmtInt, fmtTime, modal, confirmDialog } from '../ui.js';
import { facetPanel } from '../components/facets.js';
import { setScope } from '../scope.js';
import { S, hooks, refs } from './process-state.js';

/** 把「当前在看哪批样品」告诉 AI 抽屉。抽屉不该来读这里的私有 S。 */
export function publishScope() {
  setScope({ filter: S.filter, total: S.total,
             checked: [...S.checked], page: 'process' });
}

export function setFilter(next) {
  S.filter = next;
  S.checked = new Set();
  hooks.reload();
}

export async function loadSets() {
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

export function drawFacets() {
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
    h('div.facet-head', h('span.facet-title', '样品集'),
      h('button.linklike.xsmall', { style: { marginLeft: 'auto' }, onclick: () => manageSetsDialog() }, '管理')),
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

// ------------------------------------------------------------------ 样品集：管理
/**
 * 改名、钉死（动态 → 固定）、删除。三件事都只动「样品集」这个名字和它记的东西，
 * 样品和处理结果一个都不动 —— 弹窗里写明了，删之前还要再确认一次。
 */
function manageSetsDialog() {
  const list = h('div.set-manage');
  const draw = () => {
    if (!S.sets.length) {
      mount(list, h('p.small.dim', '没有样品集了。在样品列表上方点「存为样品集」新建。'));
      return;
    }
    mount(list, ...S.sets.map((s) => setRow(s, async () => { await loadSets(); draw(); })));
  };
  draw();
  const m = modal({
    title: '管理样品集',
    width: '680px',
    body: h('div',
      h('p.xsmall.dim',
        '这里只改样品集本身（名字、记下的筛选式或快照）。样品、文件、处理结果都不会动。'),
      list),
    foot: [h('button.btn', { onclick: () => m.close() }, '关闭')],
  });
}

function setRow(s, refresh) {
  const name = h('input.input.input-sm', { value: s.name, 'aria-label': `「${s.name}」的名字`, style: { width: '180px' } });
  const act = async (btn, fn, ok) => {
    busy(btn, true);
    try { await fn(); toast(ok, 'ok'); await refresh(); }
    catch (err) { busy(btn, false); toast(err.message, 'err', 6000); }
  };
  const renameBtn = h('button.btn.btn-sm', {
    onclick: (e) => {
      const v = name.value.trim();
      if (v === s.name) return;
      act(e.currentTarget, () => api.renameSet(s.set_id, v), `已改名为「${v}」`);
    },
  }, '改名');
  name.addEventListener('keydown', (e) => { if (e.key === 'Enter') renameBtn.click(); });
  return h('div.set-row', { dataset: { setId: s.set_id } },
    h('div.row.gap-2', { style: { alignItems: 'center' } },
      name, renameBtn,
      h(`span.tag${s.kind === 'pinned' ? '' : '.tag-accent'}`, s.kind === 'pinned' ? '固定集' : '动态集'),
      h('span.small', `${fmtInt(s.count)} 个样品`),
      h('span.grow'),
      s.kind === 'dynamic'
        ? h('button.btn.btn-sm', {
            title: '把此刻命中的样品钉死成固定集，以后不再随新数据变',
            onclick: async (e) => {
              const btn = e.currentTarget;
              const yes = await confirmDialog(
                `把「${s.name}」钉死成固定集？此刻命中的 ${fmtInt(s.count)} 个样品会被记下来，`
                + '以后新导入的样品不会再进来。'
                + '这一步不能撤回（要动态的话可以用原来的筛选式再建一个）。',
                { confirmLabel: '钉死' });
              if (yes) act(btn, () => api.freezeSet(s.set_id), `「${s.name}」已钉死，${fmtInt(s.count)} 个样品`);
            },
          }, '钉死')
        : null,
      h('button.btn.btn-sm.btn-danger', {
        onclick: async (e) => {
          const btn = e.currentTarget;
          const yes = await confirmDialog(
            `删除样品集「${s.name}」？只删这个名字和它记的${s.kind === 'pinned' ? '快照' : '筛选式'}，`
            + `里面的 ${fmtInt(s.count)} 个样品和它们的结果都还在。`,
            { confirmLabel: '删除', danger: true });
          if (yes) act(btn, () => api.deleteSet(s.set_id), `已删除「${s.name}」`);
        },
      }, '删除')),
    h('div.xsmall.dim.mt-1', s.kind === 'pinned'
      ? `快照 · 建于 ${fmtTime(s.created_at)}${s.updated_at !== s.created_at ? ` · 改于 ${fmtTime(s.updated_at)}` : ''}`
      : `筛选式 ${describeFilter(s.filter)} · 新导入的符合条件的样品会自动进来`));
}

/** 筛选式说成人话（只说常见的几样，其余原样给）。 */
function describeFilter(f) {
  const parts = [];
  if (f.batch?.length) parts.push(`批次 ${f.batch.join('、')}`);
  if (f.q) parts.push(`名字含「${f.q}」`);
  if (f.ids?.length) parts.push(`指定的 ${fmtInt(f.ids.length)} 个`);
  const rest = Object.keys(f).filter((k) => !['batch', 'q', 'ids'].includes(k));
  if (rest.length) parts.push(rest.join(' / '));
  return parts.join(' · ') || '（全部）';
}

// ------------------------------------------------------------------ 样品集：新建
export function saveSetDialog() {
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
