// 字段表里共用的两格：字段名（下面一行中文名）、单位（不止一个单位时提醒）。
//
// 总览、存储、构效关系三页都列字段，原来各写一份：中文名只有存储页显示，
// 同一个字段换了单位就列两行（自查 §6 #28）。后端现在一个字段只给一行，
// 不同单位都在 `units` 里；这两格负责把它说清楚。

import { h, fmtInt } from '../ui.js';

export function fieldNameCell(f) {
  return h('td', h('span.mono.strong', f.field_name),
    f.label ? h('div.xsmall.dim', f.label) : null);
}

export function unitCell(f) {
  if (!f.mixed_units) return h('td.small.muted', f.unit || '—');
  return h('td.small', {
    title: '同一个字段出现了不止一个单位。旁边的最小 / 最大是把不同单位的数混在一起算的，'
      + '不能当真 —— 多半是某个模块（或某一版）把单位写得不一致。',
  }, h('span.muted', f.unit), ' ', h('span.tag.tag-warn', '单位不一致'));
}

const range = (v) => (v === null || v === undefined ? '—' : Number(v).toPrecision(4));

/**
 * 字段表（总览、存储、构效关系三页共用；原来三份，列也不一样 —— 技术债 C076）。
 * @param numeric 多一列「其中数值」（有的字段只有一部分值是数）
 */
export function fieldTable(fields, { numeric = false } = {}) {
  return h('table.data',
    h('thead', h('tr',
      h('th', '字段'), h('th', '单位'), h('th.num', '条数'),
      numeric ? h('th.num', '其中数值') : null,
      h('th.num', '最小'), h('th.num', '最大'))),
    h('tbody', ...fields.map((f) => h('tr',
      fieldNameCell(f),
      unitCell(f),
      h('td.num', fmtInt(f.n)),
      numeric ? h('td.num.small.muted', fmtInt(f.n_numeric)) : null,
      h('td.num.small', range(f.min_v)),
      h('td.num.small', range(f.max_v))))));
}
