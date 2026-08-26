// 构效关系：这一期只保留入口，如实说明它在等什么。
// 不做一个假的分析界面。

import { api } from '../api.js';
import { h, render, fmtInt, empty } from '../ui.js';

export const meta = {
  id: 'relation',
  title: '构效关系',
  desc: '从已确认的关键字段动态定义 X / Y。当前阶段只保留入口。',
};

export function view(host, { nav }) {
  return render(host, () => api.fields(), ({ fields }) => {
    const numeric = fields.filter((f) => f.n_numeric > 0);

    return h('div',
      h('div.section',
        h('p.small.measure', { style: { lineHeight: 1.75 } },
          '这一层要等「数据处理 + 关键结果存储」稳定之后再做。',
          '它不会新建一套数据模型 —— 直接对 ',
          h('span.mono', 'key_result'),
          ' 长表按 ',
          h('span.mono', 'field_name'),
          ' 透视，就能把任意字段当 X 或 Y。',
          '这也是为什么字段角色没有写死成数据库列：新增一个测量量，不需要改表结构。')),

      h('div.section',
        h('div.section-head',
          h('div.section-title', '目前积累的可分析字段'),
          h('div.section-note', numeric.length ? `${numeric.length} 个数值字段可用` : '')),
        numeric.length
          ? h('div.panel.panel-body.flush',
              h('table.data',
                h('thead', h('tr',
                  h('th', '字段'), h('th', '单位'), h('th.num', '样本数'),
                  h('th.num', '范围'))),
                h('tbody', ...numeric.slice(0, 40).map((f) => h('tr',
                  h('td', h('span.mono.strong', f.field_name)),
                  h('td.small.muted', f.unit || '—'),
                  h('td.num', fmtInt(f.n_numeric)),
                  h('td.num.small.muted',
                    f.min_v === null ? '—'
                      : `${Number(f.min_v).toPrecision(3)} — ${Number(f.max_v).toPrecision(3)}`))))))
          : h('div.panel', empty(
              '还没有可分析的数值字段 —— 先去处理几个文件，结果会自动出现在这里',
              h('button.btn.btn-sm', { onclick: () => nav('process') }, '去处理数据')))),

      h('div.section',
        h('div.section-head', h('div.section-title', '这一层将来会做什么')),
        h('div.panel.panel-body.flush',
          ...[
            ['X / Y 动态映射', '任选两个字段做散点，按样品/批次分组着色'],
            ['相关性', '皮尔逊 / 斯皮尔曼，配合显著性，不只给一个数'],
            ['分组比较', '按批次、工艺条件分组，看分布差异'],
            ['异常识别', '把偏离趋势的样品挑出来，回溯到原始文件'],
            ['总结', '把上面这些结论交给模型写成一段可读的说明'],
          ].map(([t, d], i, arr) => h('div', {
            style: { padding: 'var(--s3x) var(--s4x)',
                     borderBottom: i === arr.length - 1 ? '0' : '1px solid var(--line)' },
          },
            h('div.row.gap-3', h('span.strong.small', t), h('span.small.muted', d)))))));
  });
}
