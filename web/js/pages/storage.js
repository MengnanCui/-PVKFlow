// 数据存储：展示真实的落地情况，而不是一张架构示意图。

import { api } from '../api.js';
import { h, mount, clear, render, fmtBytes, fmtInt, fmtTime, empty } from '../ui.js';

export const meta = {
  id: 'storage',
  title: '数据存储',
  // 跟数据处理页的分工写在这儿，省得两个页面看起来像在做同一件事：
  // 数据处理是**挑一批出来跑**，数据存储是**全部东西在哪儿、有多少**。
  desc: '全部数据在这里。数据处理页是挑一批出来跑，这里是查它们都存到哪儿了',
};

export function view(host, ctx = {}) {
  return render(host, () => api.storageStats(), (d) => {
    const modes = Object.fromEntries(d.by_mode.map((m) => [m.storage_mode, m]));
    const copied = modes.copied || { n: 0, bytes: 0 };
    const referenced = modes.referenced || { n: 0, bytes: 0 };

    return h('div',
      searchSection(ctx),

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

// ------------------------------------------------------------------ 全库搜索
//
// 数据处理页的搜索是**在当前筛选式之内**找；这里是**全库**找 ——
// 「ZG0014 那次测量到底在不在库里」这种问题该在这一页得到答案。
//
// 走的是已有的 /api/selection/query，`q` 键本来就同时匹配样品号和样品名，
// 不为这一个框新写查询。
const SEARCH_LIMIT = 50;
let searchTimer = null;

function searchSection(ctx) {
  const resultHost = h('div#storageSearchResult');

  const input = h('input.input', {
    type: 'search', placeholder: '搜样品名或样品号，例如 ZG0014',
    style: { width: '100%' },
    oninput: (e) => {
      clearTimeout(searchTimer);
      const q = e.target.value.trim();
      // 每敲一个字打一次后端太吵；停 250ms 再查
      searchTimer = setTimeout(() => runSearch(q, resultHost, ctx), 250);
    },
  });

  return h('div.section',
    h('div.section-head',
      h('div.section-title', '找一个样品'),
      h('div.section-note', '在全部数据里找，不受任何筛选限制')),
    h('div.panel.panel-body',
      input,
      resultHost));
}

async function runSearch(q, host, ctx) {
  if (!q) { clear(host); return; }

  let d;
  try {
    d = await api.selectionQuery({ filter: { q }, limit: SEARCH_LIMIT });
  } catch (err) {
    mount(host, h('div.mt-3.small', { style: { color: 'var(--danger)' } }, err.message));
    return;
  }

  if (!d.rows.length) {
    mount(host, h('div.mt-3.small.muted', `没有匹配「${q}」的样品。`));
    return;
  }

  mount(host,
    h('div.mt-3.row-between',
      h('span.small.muted',
        d.total > d.rows.length
          // 截断了就说出来，别让人以为这就是全部
          ? `命中 ${fmtInt(d.total)} 个，下面是前 ${fmtInt(d.rows.length)} 个`
          : `命中 ${fmtInt(d.total)} 个`)),
    h('table.data.mt-2',
      h('thead', h('tr',
        h('th', '样品'), h('th', '样品号'), h('th', '测量时间'),
        h('th.num', '文件'), h('th.num', '结果'), h('th', ''))),
      h('tbody', ...d.rows.map((r) => h('tr',
        h('td.strong.truncate', { title: r.name }, r.name),
        h('td.small.muted', r.batch || '—'),
        h('td.small.muted', r.measured_at ? fmtTime(r.measured_at) : '—'),
        h('td.num', fmtInt(r.n_files)),
        h('td.num', fmtInt(r.n_results)),
        h('td.num',
          r.matrix_id
            ? h('button.btn.btn-sm', {
                onclick: () => ctx.nav?.('sample', { arg: r.matrix_id }),
              }, '打开')
            : h('span.xsmall.dim', '无光谱')))))));
}
