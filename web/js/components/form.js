// 把 skill 声明的 ParamSpec 渲染成表单。
// 这是「加一个 skill，界面免费出现」的关键一环——skill 作者不需要碰前端。

import { h } from '../ui.js';

export function paramForm(params, initial = {}, { columns = [] } = {}) {
  const values = {};
  const controls = [];

  for (const p of params || []) {
    const start = initial[p.key] !== undefined ? initial[p.key] : p.default;
    values[p.key] = start;
    const set = (v) => { values[p.key] = v; };
    let control;

    switch (p.type) {
      case 'bool': {
        const sw = h('button.switch', {
          type: 'button', role: 'switch', 'aria-checked': String(!!start),
          onclick: () => {
            const next = sw.getAttribute('aria-checked') !== 'true';
            sw.setAttribute('aria-checked', String(next));
            set(next);
          },
        });
        control = h('div.row-between', h('span.small', p.label), sw);
        break;
      }
      case 'select': {
        control = labelled(p, h('select.select', {
          onchange: (e) => set(e.target.value),
        }, ...(p.options || []).map((o) =>
          h('option', { value: o, selected: o === start }, String(o)))));
        break;
      }
      case 'column':
      case 'columns': {
        const multi = p.type === 'columns';
        const sel = h('select.select', {
          multiple: multi,
          size: multi ? Math.min(5, Math.max(3, columns.length)) : undefined,
          onchange: (e) => set(multi
            ? [...e.target.selectedOptions].map((o) => o.value)
            : (e.target.value || null)),
        },
          multi ? null : h('option', { value: '' }, '自动选择'),
          ...columns.map((c) => h('option', { value: c, selected: multi
            ? Array.isArray(start) && start.includes(c)
            : start === c }, c)));
        control = labelled(p, sel, columns.length ? null : '先选一个文件，列名会出现在这里');
        break;
      }
      case 'range': {
        const arr = Array.isArray(start) ? start : [null, null];
        control = labelled(p, h('div.range-pair',
          h('input.input', { type: 'number', value: arr[0] ?? '', placeholder: '起',
            oninput: (e) => { const v = values[p.key] || [null, null];
                              set([num(e.target.value), v[1]]); } }),
          h('span.sep', '至'),
          h('input.input', { type: 'number', value: arr[1] ?? '', placeholder: '止',
            oninput: (e) => { const v = values[p.key] || [null, null];
                              set([v[0], num(e.target.value)]); } })));
        break;
      }
      case 'number': {
        control = labelled(p, h('input.input', {
          type: 'number', value: start ?? '', min: p.min ?? undefined,
          max: p.max ?? undefined, step: p.step ?? 'any',
          oninput: (e) => set(num(e.target.value)),
        }));
        break;
      }
      default: {
        control = labelled(p, h('input.input', {
          type: 'text', value: start ?? '', placeholder: p.help || '',
          oninput: (e) => set(e.target.value),
        }));
      }
    }
    controls.push(control);
  }

  const node = controls.length
    ? h('div.col.gap-3', ...controls)
    : h('p.small.muted', '这个处理不需要参数。');

  return { node, values, get: () => ({ ...values }) };
}

function labelled(p, control, extraHelp) {
  return h('div.field',
    h('label.field-label', p.label, p.unit ? h('span.unit', ` (${p.unit})`) : null),
    control,
    (p.help || extraHelp) ? h('div.field-help', extraHelp || p.help) : null);
}

const num = (v) => (v === '' || v === null ? null : Number(v));
