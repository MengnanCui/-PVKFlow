// 数据处理页：筛选 —— 搜索框、分面、保存的样品集。
// 筛选一变就重新查询（经 hooks.reload，见 process-state.js）。

import { api } from '../api.js';
import { h, mount, toast, skeletonRows, busy, fmtInt, modal } from '../ui.js';
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

// ------------------------------------------------------------------ 样品集
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
