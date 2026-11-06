// 字段表里共用的两格：字段名（下面一行中文名）、单位（不止一个单位时提醒）。
//
// 总览、存储、构效关系三页都列字段，原来各写一份：中文名只有存储页显示，
// 同一个字段换了单位就列两行（自查 §6 #28）。后端现在一个字段只给一行，
// 不同单位都在 `units` 里；这两格负责把它说清楚。

import { h } from '../ui.js';

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
