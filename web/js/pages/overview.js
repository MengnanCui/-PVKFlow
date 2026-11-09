// 总览：只显示数据库里真实存在的数字。为零就是零，不编。

import { api } from '../api.js';
import { withInfo } from '../components/info.js';
import { fieldNameCell, unitCell } from '../components/field-cells.js';
import { h, render, fmtInt, fmtTime, empty, sampleLabel, statusText, statusClass } from '../ui.js';

export const meta = {
  id: 'overview',
  title: '总览',
  desc: '处理 → 存储 → 构效关系。当前阶段的重点是前两步。',
};

export function actions(nav) {
  return [h('button.btn.btn-primary', { onclick: () => nav('process') }, '开始处理数据')];
}

export function view(host, { nav }) {
  return render(host, () => api.overview(), (data) => {
    const c = data.counts;
    const empty0 = c.artifacts === 0;

    return h('div',
      h('div.section',
        metricRow([
          [withInfo('样品', 'sample_identity'), c.samples, '实验对象'],
          [withInfo('原始文件', 'storage_mode'), c.artifacts, '已登记'],
          ['关键结果', c.results, '结构化字段值'],
          ['待复核', c.pending_review, '处理后需人工确认'],
        ])),

      (c.missing_files || c.failed_runs)
        ? h('div.section',
            c.missing_files ? h('div.notice.notice-warn',
              h('div.grow', `有 ${c.missing_files} 个原始文件在记录的位置找不到了。`,
                h('span.muted', ' 多半是引用型文件（只登记了路径）的原文件被挪走了。')),
              h('button.btn.btn-sm', { onclick: () => nav('storage') }, '去看是哪些')) : null,
            c.failed_runs ? h('div.notice.notice-warn.mt-2',
              h('div.grow', `有 ${c.failed_runs} 次处理失败。`)) : null)
        : null,

      empty0 ? h('div.section', firstRun(nav)) : null,

      h('div.section',
        h('div.section-head',
          h('div.section-title', '三层结构'),
          h('div.section-note', '时间上先后，能力上独立')),
        h('div', stageList(nav, c))),

      h('div.section',
        h('div.section-head',
          h('div.section-title', '最近的处理'),
          data.recent_runs.length
            ? h('button.btn.btn-sm', { onclick: () => nav('process') }, '去处理')
            : null),
        data.recent_runs.length
          ? h('div.panel.panel-body.flush', runTable(data.recent_runs))
          : h('div.panel', empty('还没有跑过任何处理'))),

      data.fields.length
        ? h('div.section',
            h('div.section-head',
              h('div.section-title', '已有的关键字段'),
              h('div.section-note', data.n_fields > data.fields.length
                ? `最常见的 ${data.fields.length} 个，共 ${fmtInt(data.n_fields)} 个 · 全部见「构效关系」`
                : '构效关系分析时可选作 X / Y')),
            h('div.panel.panel-body.flush', fieldTable(data.fields)))
        : null);
  });
}

function metricRow(items) {
  return h('div.metrics.metrics-4', ...items.map(([label, value, note]) =>
    h('div.metric',
      h('div.metric-label', label),
      h('div.metric-value' + (value ? '' : '.is-zero'), fmtInt(value)),
      h('div.metric-note', note))));
}

function firstRun(nav) {
  return h('div.notice.notice-accent',
    h('div.grow',
      h('div.strong', '工作区还是空的'),
      h('p.small.mt-2',
        '先导入一批实验文件：文本类会按内容哈希复制进工作区，图像保持在原位只做登记。'),
      h('div.mt-3', h('button.btn.btn-primary.btn-sm',
        { onclick: () => nav('process') }, '去导入'))));
}

function stageList(nav, counts) {
  const stages = [
    ['01', '数据处理', 'process',
     '导入实验文件，用功能模块处理。模块是插件——放一个进来，控件、图和对比页上的那一格自动出现。',
     counts.runs ? `已跑 ${counts.runs} 次` : '尚未运行'],
    ['02', '数据存储', 'storage',
     '原始文件不丢失，分析结果结构化回写。图像、曲线、数字、文本各归其位，用统一 ID 串起来。',
     counts.artifacts ? `${fmtInt(counts.artifacts)} 个文件` : '尚无数据'],
    ['03', '构效关系', 'relation',
     '从已确认的关键字段里动态选 X / Y 做统计与比较。字段角色不写死在数据库里。',
     '后续阶段'],
  ];
  return h('div.panel.panel-body.flush',
    ...stages.map(([num, title, page, desc, note], i) =>
      h('button.list-row', {
        onclick: () => nav(page),
        style: { alignItems: 'flex-start', padding: 'var(--s4x)',
                 borderBottom: i === stages.length - 1 ? '0' : null },
      },
        h('span.mono.dim.small', { style: { paddingTop: '2px', width: '22px' } }, num),
        h('div.grow',
          h('div.row.gap-2', h('span.strong', title), h('span.xsmall.dim', note)),
          h('p.small.muted.mt-2.measure', desc)))));
}

function runTable(runs) {
  return h('table.data',
    h('thead', h('tr',
      h('th', '处理'), h('th', '样品'), h('th', '状态'),
      h('th.num', '结果数'), h('th', '版本'), h('th', '时间'))),
    h('tbody', ...runs.map((r) => h('tr',
      h('td', h('span.strong', r.skill_name || r.skill_id)),
      h('td', sampleLabel(r.sample_name, r.sample_batch)),
      h('td', h('span.status.' + statusClass(r.status), statusText(r.status))),
      h('td.num', fmtInt(r.n_results)),
      h('td.mono.xsmall.muted', r.skill_version),
      h('td.small.muted', fmtTime(r.started_at))))));
}

function fieldTable(fields) {
  return h('table.data',
    h('thead', h('tr',
      h('th', '字段'), h('th', '单位'), h('th.num', '条数'),
      h('th.num', '最小'), h('th.num', '最大'))),
    h('tbody', ...fields.map((f) => h('tr',
      fieldNameCell(f),
      unitCell(f),
      h('td.num', fmtInt(f.n)),
      h('td.num.small', f.min_v === null ? '—' : Number(f.min_v).toPrecision(4)),
      h('td.num.small', f.max_v === null ? '—' : Number(f.max_v).toPrecision(4))))));
}
