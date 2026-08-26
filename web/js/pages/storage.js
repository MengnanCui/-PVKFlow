// 数据存储：展示真实的落地情况，而不是一张架构示意图。

import { api } from '../api.js';
import { h, render, fmtBytes, fmtInt, empty } from '../ui.js';

export const meta = {
  id: 'storage',
  title: '数据存储',
  desc: '原始文件、结构化关键字段、大规模数值 —— 三层各归其位',
};

export function view(host) {
  return render(host, () => api.storageStats(), (d) => {
    const modes = Object.fromEntries(d.by_mode.map((m) => [m.storage_mode, m]));
    const copied = modes.copied || { n: 0, bytes: 0 };
    const referenced = modes.referenced || { n: 0, bytes: 0 };

    return h('div',
      h('div.section',
        h('div.metrics.metrics-4',
          metric('复制进工作区', copied.n, fmtBytes(copied.bytes) + ' · 内容寻址去重'),
          metric('原地引用', referenced.n, fmtBytes(referenced.bytes) + ' · 原图未搬动'),
          metric('关键结果', d.counts.results, `${d.fields.length} 个不同字段`),
          metric('数值表', d.tables.length, 'Parquet 列式存储'))),

      h('div.section',
        h('div.section-head',
          h('div.section-title', '为什么分三层'),
          h('div.section-note', '不同数据类型选不同容器，再用统一 ID 串起来')),
        h('div.panel.panel-body.flush', layers(copied, referenced, d))),

      h('div.section',
        h('div.section-head',
          h('div.section-title', '按类型分布'),
          h('div.section-note', '哪些扩展名被复制、哪些被引用')),
        d.by_ext.length
          ? h('div.panel.panel-body.flush', extTable(d.by_ext))
          : h('div.panel', empty('还没有导入文件'))),

      h('div.section',
        h('div.section-head',
          h('div.section-title', '关键字段目录'),
          h('div.section-note', '构效关系页的 X / Y 候选就来自这张表')),
        d.fields.length
          ? h('div.panel.panel-body.flush', fieldTable(d.fields))
          : h('div.panel', empty('还没有跑过处理，所以还没有结构化字段'))),

      d.tables.length
        ? h('div.section',
            h('div.section-head', h('div.section-title', '数值表')),
            h('div.panel.panel-body.flush',
              h('table.data',
                h('thead', h('tr', h('th', '名称'), h('th', '来自'), h('th.num', '行数'), h('th', '路径'))),
                h('tbody', ...d.tables.map((t) => h('tr',
                  h('td.strong', t.name),
                  h('td.small.muted', t.skill_id || '—'),
                  h('td.num', fmtInt(t.n_rows)),
                  h('td.mono.xsmall.dim.truncate', { title: t.path }, t.path)))))))
        : null,

      h('div.section',
        h('div.section-head', h('div.section-title', '当前阶段的存储决策')),
        h('div.panel.panel-body',
          h('p.small.measure', { style: { lineHeight: 1.7 } },
            h('span.strong', '本地混合架构：'),
            'SQLite 管索引与关系（样品、测量、分析运行、关键结果），',
            'Parquet 管大量结构化数值（曲线点、批量特征），',
            '普通文件系统保存图像与原始文件。',
            '短期零运维；等到多人协作或跨机器部署，把 SQLite 换成 PostgreSQL、',
            '文件层换成 MinIO/S3 即可，上层代码不用动 —— 因为所有调用方只认 artifact_id，',
            '不关心文件到底在哪。'),
          h('div.row.wrap.gap-3.mt-4',
            ...['sample_id — 实验对象', 'measurement_id — 一次测量',
                'analysis_run_id — 一次处理', 'artifact_id — 文件 / 图像 / 曲线']
              .map((t) => h('span.tag', t))))));
  });
}

function metric(label, value, note) {
  return h('div.metric',
    h('div.metric-label', label),
    h('div.metric-value' + (value ? '' : '.is-zero'), fmtInt(value)),
    h('div.metric-note', note));
}

function layers(copied, referenced, d) {
  const rows = [
    ['原始 / 大文件层', '文件系统',
     `文本类按 sha256 内容寻址复制进 workspace/raw/（${copied.n} 个）；` +
     `图像只登记绝对路径与哈希，原图不动（${referenced.n} 个）。`,
     '升级路线：MinIO / S3'],
    ['元数据 / 索引层', 'SQLite',
     `样品、测量、分析运行、关键结果等强关系数据，需要事务与快速检索。` +
     `当前 ${fmtInt(d.counts.results)} 条关键结果、${fmtInt(d.counts.runs)} 次处理记录。`,
     '升级路线：PostgreSQL'],
    ['数值分析层', 'Parquet',
     `曲线点与批量特征走列式存储，${d.tables.length} 张表。` +
     `适合后期按字段做筛选与统计。`,
     '升级路线：+ DuckDB 查询层'],
  ];
  return h('div', ...rows.map(([title, tech, desc, next], i) =>
    h('div', {
      style: { padding: 'var(--s4x)',
               borderBottom: i === rows.length - 1 ? '0' : '1px solid var(--line)' },
    },
      h('div.row.gap-3',
        h('span.strong', title),
        h('span.tag.tag-accent', tech),
        h('span.xsmall.dim', { style: { marginLeft: 'auto' } }, next)),
      h('p.small.muted.mt-2.measure', desc))));
}

function extTable(rows) {
  const max = Math.max(...rows.map((r) => r.n), 1);
  return h('table.data',
    h('thead', h('tr', h('th', '扩展名'), h('th.num', '文件数'), h('th.num', '总大小'),
                 h('th', '占比（按文件数）'))),
    h('tbody', ...rows.map((r) => h('tr',
      h('td', h('span.mono.strong', r.ext || '(无扩展名)')),
      h('td.num', fmtInt(r.n)),
      h('td.num.small.muted', fmtBytes(r.bytes)),
      h('td', { style: { width: '38%' } },
        h('div.bar', h('i', { style: { width: `${(r.n / max) * 100}%` } })))))));
}

function fieldTable(fields) {
  return h('table.data',
    h('thead', h('tr',
      h('th', '字段'), h('th', '单位'), h('th.num', '条数'),
      h('th.num', '其中数值'), h('th.num', '最小'), h('th.num', '最大'))),
    h('tbody', ...fields.map((f) => h('tr',
      h('td', h('span.mono.strong', f.field_name),
              f.label && f.label !== f.field_name ? h('div.xsmall.dim', f.label) : null),
      h('td.small.muted', f.unit || '—'),
      h('td.num', fmtInt(f.n)),
      h('td.num.small.muted', fmtInt(f.n_numeric)),
      h('td.num.small', f.min_v === null ? '—' : Number(f.min_v).toPrecision(4)),
      h('td.num.small', f.max_v === null ? '—' : Number(f.max_v).toPrecision(4))))));
}
