// 分面筛选面板。
//
// 核心：**一次选择是一个筛选式，不是一串 ID**。这个组件只负责把筛选式
// 画出来、让人改，改完把新的筛选式交回去。
//
// 计数是在「去掉自己这一面之后」的筛选式下算的 —— 选了 B20 之后其他批次
// 的计数还在，所以还能改选。这一条在后端保证（selection.facets）。

import { h, mount, fmtInt } from '../ui.js';

/**
 * facetPanel({ facets, filter, onChange })
 * onChange(newFilter) 在任何一次改动后触发。
 */
export function facetPanel({ facets, filter, onChange }) {
  const f = { ...filter };

  const toggle = (key, value) => {
    const cur = new Set(f[key] || []);
    cur.has(value) ? cur.delete(value) : cur.add(value);
    const next = [...cur];
    if (next.length) f[key] = next; else delete f[key];
    onChange({ ...f });
  };

  const sections = [];

  if (facets.name?.patterns?.length) sections.push(nameSection(f, facets.name, onChange));
  if (facets.batch?.length) sections.push(chipSection('批次', 'batch', facets.batch, f, toggle));
  if (facets.folder?.length) sections.push(chipSection('文件夹', 'folder', facets.folder, f, toggle));
  if (facets.method?.length) sections.push(chipSection('测量方法', 'method', facets.method, f, toggle));
  if (facets.field?.length) sections.push(fieldSection(f, facets.field, onChange));
  if (facets.import?.length) {
    sections.push(chipSection('导入批次', 'import',
      facets.import.map((i) => ({ ...i, value: i.value, label: i.label })), f, toggle));
  }

  return h('div.facets', ...sections);
}

function chipSection(title, key, items, f, toggle) {
  const selected = new Set(f[key] || []);
  return h('div.facet',
    h('div.facet-head',
      h('span.facet-title', title),
      selected.size
        ? h('button.facet-clear', {
            onclick: () => { selected.forEach((v) => toggle(key, v)); },
          }, '清除')
        : null),
    h('div.facet-chips', ...items.map((it) =>
      h('button.chip-toggle', {
        'aria-pressed': String(selected.has(String(it.value))),
        disabled: it.count === 0 && !selected.has(String(it.value)),
        onclick: () => toggle(key, String(it.value)),
        title: it.label && it.label !== it.value ? it.label : undefined,
      },
        h('span.chip-label', it.label || it.value),
        h('span.chip-count', fmtInt(it.count))))));
}

/**
 * 名称范围 —— 这一条是为了回答「不想先去别处读范围再回来输」。
 * 滑块的两个端点就是数据里的真实 min/max，范围本身就画在那儿。
 */
function nameSection(f, nameFacet, onChange) {
  const patterns = nameFacet.patterns;
  const active = f.name_range;
  const current = active
    ? patterns.find((p) => p.prefix === active.prefix && p.suffix === active.suffix)
    : patterns[0];
  if (!current) return h('div');

  const lo = active?.min ?? current.min;
  const hi = active?.max ?? current.max;

  const loNum = h('input.input.input-sm', { type: 'number', value: lo,
    min: current.min, max: current.max, style: { width: '68px' } });
  const hiNum = h('input.input.input-sm', { type: 'number', value: hi,
    min: current.min, max: current.max, style: { width: '68px' } });
  const loRange = h('input.range', { type: 'range', value: lo,
    min: current.min, max: current.max, step: 1 });
  const hiRange = h('input.range', { type: 'range', value: hi,
    min: current.min, max: current.max, step: 1 });

  const emit = () => {
    let a = Number(loNum.value), b = Number(hiNum.value);
    if (a > b) [a, b] = [b, a];
    loNum.value = loRange.value = a;
    hiNum.value = hiRange.value = b;
    const full = a <= current.min && b >= current.max;
    const next = { ...f };
    if (full) delete next.name_range;
    else next.name_range = { prefix: current.prefix, suffix: current.suffix, min: a, max: b };
    onChange(next);
  };
  const sync = (src, dst) => (e) => { dst.value = e.target.value; };
  loNum.oninput = sync(loNum, loRange); hiNum.oninput = sync(hiNum, hiRange);
  loRange.oninput = sync(loRange, loNum); hiRange.oninput = sync(hiRange, hiNum);
  [loNum, hiNum, loRange, hiRange].forEach((el) => { el.onchange = emit; });

  const picker = patterns.length > 1
    ? h('select.select.select-sm', {
        onchange: (e) => {
          const p = patterns[Number(e.target.value)];
          onChange({ ...f, name_range: { prefix: p.prefix, suffix: p.suffix,
                                         min: p.min, max: p.max } });
        },
      }, ...patterns.map((p, i) => h('option', {
        value: i, selected: p.prefix === current.prefix && p.suffix === current.suffix,
      }, `${p.prefix}◻${p.suffix}（${p.count} 个）`)))
    : null;

  return h('div.facet',
    h('div.facet-head',
      h('span.facet-title', '名称范围'),
      active ? h('button.facet-clear', {
        onclick: () => { const n = { ...f }; delete n.name_range; onChange(n); },
      }, '清除') : null),
    picker ? h('div.mt-2', picker) : null,
    h('div.range-row.mt-2',
      h('span.mono.xsmall.dim', current.prefix || '—'),
      loNum, h('span.small.dim', '–'), hiNum,
      current.suffix ? h('span.mono.xsmall.dim', current.suffix) : null),
    h('div.band-sliders.mt-2', loRange, hiRange),
    h('div.xsmall.dim.mt-2',
      `数据里是 ${current.prefix}${current.min}–${current.max}${current.suffix}`,
      current.complete ? '' : '（中间有缺号）'));
}

function fieldSection(f, fields, onChange) {
  const chosen = new Map((f.field || []).map((x) => [x.name, x]));
  return h('div.facet',
    h('div.facet-head',
      h('span.facet-title', '关键结果'),
      chosen.size ? h('button.facet-clear', {
        onclick: () => { const n = { ...f }; delete n.field; onChange(n); },
      }, '清除') : null),
    ...fields.slice(0, 8).map((fd) => {
      const sel = chosen.get(fd.name);
      const lo = h('input.input.input-sm', { type: 'number', placeholder: fmtRange(fd.min),
        value: sel?.min ?? '', style: { width: '100%' } });
      const hi = h('input.input.input-sm', { type: 'number', placeholder: fmtRange(fd.max),
        value: sel?.max ?? '', style: { width: '100%' } });
      const emit = () => {
        const others = (f.field || []).filter((x) => x.name !== fd.name);
        const a = lo.value === '' ? null : Number(lo.value);
        const b = hi.value === '' ? null : Number(hi.value);
        const next = { ...f };
        if (a === null && b === null) {
          if (others.length) next.field = others; else delete next.field;
        } else {
          next.field = [...others, { name: fd.name, min: a, max: b }];
        }
        onChange(next);
      };
      lo.onchange = emit; hi.onchange = emit;
      // 名字单独一行：integral_initial 和 integral_final 挤在输入框旁边会被
      // 截成一样的 "integ…"，五行长得完全一样，等于没写。
      return h('div.field-row',
        h('div.field-name',
          h('span.mono.small', fd.name),
          fd.unit ? h('span.xsmall.dim', ` ${fd.unit}`) : null,
          h('span.xsmall.dim', ` · ${fmtInt(fd.count)}`)),
        h('div.row.gap-1', lo, h('span.xsmall.dim', '–'), hi));
    }));
}

const fmtRange = (v) => (v === null || v === undefined ? '' :
  Math.abs(v) >= 1e4 || (Math.abs(v) < 1e-2 && v !== 0)
    ? Number(v).toExponential(1) : Number(v).toPrecision(3));

/** 当前筛选式的摘要 chip 行 —— 让人一眼看出「我现在选的是什么」。 */
export function filterSummary(filter, onChange) {
  const f = filter || {};
  const chips = [];
  const drop = (key, value) => {
    const next = { ...f };
    if (value === undefined) delete next[key];
    else {
      const left = (next[key] || []).filter((v) => v !== value);
      if (left.length) next[key] = left; else delete next[key];
    }
    onChange(next);
  };

  const push = (label, key, value) => chips.push(
    h('button.filter-chip', { onclick: () => drop(key, value), title: '点击移除' },
      label, h('span.filter-chip-x', '×')));

  for (const [key, title] of [['batch', '批次'], ['folder', '文件夹'],
                              ['method', '方法'], ['import', '导入']]) {
    for (const v of f[key] || []) push(`${title} ${v}`, key, v);
  }
  if (f.name_range) {
    const r = f.name_range;
    push(`名称 ${r.prefix}${r.min}–${r.max}${r.suffix || ''}`, 'name_range');
  }
  for (const fd of f.field || []) {
    const range = [fd.min ?? '', fd.max ?? ''].join('–');
    push(`${fd.name} ${range}`, 'field', fd);
  }
  if (f.q) push(`搜索「${f.q}」`, 'q');
  if (f.ids) push(`手选 ${f.ids.length} 个`, 'ids');
  if (f.exclude) push(`排除 ${f.exclude.length} 个`, 'exclude');

  if (!chips.length) return h('span.small.dim', '没有筛选，全部样品');
  return h('div.filter-chips',
    ...chips,
    h('button.filter-chip.is-clear', { onclick: () => onChange({}) }, '全部清除'));
}
