// 构效关系：任选两个字段看相关，按批次看分布。
//
// 不另建数据模型 —— 后端把关键结果长表按字段透视成每个样品一行（app/storage/relation.py），
// 读的是「当前值」：同一个样品跑过三次批处理，图上也只有一个点。
// 样品范围和数据处理页是同一个筛选式：那一页筛出什么，这里就分析什么。
//
// 第一版做三件事：散点（按批次着色）+ 皮尔逊 / 斯皮尔曼（带 p 值）、按批次的箱线图、
// 全部可用字段的清单。异常识别、让模型写总结还没做（页面底部写着）。

import { api } from '../api.js';
import {
  h, mount, render, empty, errorBox, skeletonRows, fmtInt, latest,
} from '../ui.js';
import { figure, selectControl } from '../components/figure.js';
import { fieldTable } from '../components/field-cells.js';
import { scatterChart, boxPlot } from '../components/relation-charts.js';
import { downloadMenu } from '../download.js';
import { rowsToCsv } from '../csv.js';
import { getScope } from '../scope.js';
import { qualityControl } from '../components/quality.js';

export const meta = {
  id: 'relation',
  title: '构效关系',
  desc: '任选两个字段看相关，按批次看分布',
};

// 上次选的两个字段记在这台电脑的浏览器里 —— 只是个方便，读不到就用默认
const KEY = 'hte.relation.choice';
const remember = () => { try { localStorage.setItem(KEY, JSON.stringify({ x: S.x, y: S.y, g: S.g })); } catch { /* 隐私模式 */ } };
const recall = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch { return {}; } };

const S = { fields: [], x: null, y: null, g: null, scope: 'all', pairs: null, groups: null, tableOpen: false };
const refs = {};
const pairsTicket = latest();
const groupsTicket = latest();

export function view(host, ctx) {
  refs.nav = ctx.nav;
  refs.alive = ctx.alive || (() => true);
  return render(host, () => api.fields(), ({ fields }) => {
    S.fields = fields.filter((f) => f.n_numeric > 0);
    if (S.fields.length < 2) {
      return h('div.panel', empty(
        S.fields.length
          ? '只有一个数值字段 —— 至少要两个才谈得上「关系」。先去跑一次批处理'
          : '还没有可分析的数值字段 —— 先去处理几个文件，结果会自动出现在这里',
        h('button.btn.btn-sm', { onclick: () => ctx.nav('process') }, '去处理数据')));
    }
    const names = new Set(S.fields.map((f) => f.field_name));
    const saved = recall();
    S.x = names.has(saved.x) ? saved.x : S.fields[0].field_name;
    S.y = names.has(saved.y) && saved.y !== S.x ? saved.y
      : S.fields.find((f) => f.field_name !== S.x).field_name;
    S.g = names.has(saved.g) ? saved.g : S.y;

    refs.pairsHost = h('div');
    refs.groupsHost = h('div');
    const page = h('div',
      h('div.section', refs.pairsHost),
      h('div.section', refs.groupsHost),
      fieldsSection(),
      laterSection());
    loadPairs();
    loadGroups();
    return page;
  });
}

// ------------------------------------------------------------------ 范围
/** 和数据处理页同一个筛选式。那一页还没打开过时只有「全部」。 */
function scopeOptions() {
  const sc = getScope();
  const opts = [['all', '全部样品']];
  if (sc.filter && Object.keys(sc.filter).length) opts.push(['filter', `数据处理页筛出的（${fmtInt(sc.total)} 个）`]);
  if (sc.checked?.length) opts.push(['checked', `数据处理页勾选的（${fmtInt(sc.checked.length)} 个）`]);
  if (!opts.some(([v]) => v === S.scope)) S.scope = 'all';
  return opts;
}
function scopeFilter() {
  const sc = getScope();
  if (S.scope === 'filter') return sc.filter || {};
  if (S.scope === 'checked') return { ids: sc.checked || [] };
  return {};
}

const fieldOptions = () => S.fields.map((f) => [f.field_name,
  `${f.label || f.field_name}${f.unit ? `（${f.unit}）` : ''} · ${fmtInt(f.n_numeric)}`]);
const axisLabel = (f) => `${f.label || f.field_name}${f.unit ? ` (${f.unit})` : ''}`;

// ------------------------------------------------------------------ 散点 + 相关
async function loadPairs({ quiet = false } = {}) {
  const host = refs.pairsHost;
  const fresh = pairsTicket();
  // 审核完刷新时不闪骨架屏：表格是展开的、人正看着那一行，整块换成占位再换回来会跳
  if (!quiet) mount(host, skeletonRows(6));
  try {
    const d = await api.relationPairs({ x: S.x, y: S.y, filter: scopeFilter() });
    if (!refs.alive() || !fresh()) return;
    S.pairs = d;
    drawPairs();
  } catch (err) {
    if (!refs.alive() || !fresh()) return;
    mount(host, errorBox(err, loadPairs));
  }
}

function drawPairs() {
  const d = S.pairs;
  const chart = scatterChart({
    points: d.points.map((p) => ({ ...p, label: p.name, group: p.batch, id: p.sample_id })),
    x_label: axisLabel(d.x), y_label: axisLabel(d.y), fit: d.stats.pearson ? d.stats.fit : null,
  }, { onPick: (p) => refs.nav('sample', { arg: p.sample_id }) });

  const ctl = [
    selectControl('X', S.x, fieldOptions(), (v) => { S.x = v; remember(); loadPairs(); }),
    h('button.btn.btn-sm.btn-ghost', {
      title: '交换 X 和 Y',
      onclick: () => { [S.x, S.y] = [S.y, S.x]; remember(); loadPairs(); },
    }, '⇄'),
    selectControl('Y', S.y, fieldOptions(), (v) => { S.y = v; remember(); loadPairs(); }),
    selectControl('范围', S.scope, scopeOptions(), (v) => { S.scope = v; loadPairs(); loadGroups(); }),
  ];
  const dl = downloadMenu({
    svg: () => chart.querySelector('svg'),
    csv: () => rowsToCsv(['样品', '批次', axisLabel(d.x), axisLabel(d.y)],
                         d.points.map((p) => [p.name, p.batch, p.x, p.y])),
    name: `构效关系_${d.y.field_name}_vs_${d.x.field_name}`,
  });

  mount(refs.pairsHost,
    figure(`${axisLabel(d.y)}  vs  ${axisLabel(d.x)}`, {
      head: dl, ctl, body: chart,
      note: h('div', statsPanel(d), pointTable(d)),
    }));
}

/** 相关系数和它的说明。每个数都可能没有 —— 那时候说为什么没有，不给一个看起来正常的数。 */
function statsPanel(d) {
  const st = d.stats;
  // 「p = 3.9e-15」；小到双精度表示不了（算出来是 0）时写「p < 1e-300」，不写「p = 0」
  const p = (v) => (v === 0 ? 'p < 1e-300' : `p = ${v < 0.001 ? v.toExponential(1) : v.toFixed(3)}`);
  const r3 = (v) => (Math.abs(v) >= 0.9995 ? v.toFixed(4) : v.toFixed(3));
  const warn = [];
  for (const f of [d.x, d.y]) {
    if (f.mixed_units) {
      warn.push(`「${f.label || f.field_name}」在不同样品上单位不一致（${f.units.join(' / ')}），`
        + '这里的相关不可信 —— 多半是某个模块的单位写得不一致');
    }
  }
  const missing = [];
  if (d.n_only_x) missing.push(`${fmtInt(d.n_only_x)} 个只有 X`);
  if (d.n_only_y) missing.push(`${fmtInt(d.n_only_y)} 个只有 Y`);
  if (d.n_neither) missing.push(`${fmtInt(d.n_neither)} 个两个都没有`);

  return h('div.relation-stats',
    h('div.small', h('strong', `${fmtInt(st.n)} 个样品`), ' 同时有这两个数',
      missing.length ? `（范围里共 ${fmtInt(d.n_scope)} 个：另有 ${missing.join('、')}，不在图上）` : ''),
    st.pearson
      ? h('div.relation-nums',
          h('div', h('span.dim', '皮尔逊 r '), h('strong', r3(st.pearson.r)),
            st.pearson.ci95 ? h('span.dim', `  95% 置信区间 ${r3(st.pearson.ci95[0])} – ${r3(st.pearson.ci95[1])}`) : null,
            h('span.dim', `  ${p(st.pearson.p)}`)),
          h('div', h('span.dim', '斯皮尔曼 ρ '), h('strong', r3(st.spearman.rho)),
            h('span.dim', `  ${p(st.spearman.p)}`),
            st.spearman.approx ? h('span.xsmall.dim', '  （样品少于 10 个，这个 p 是近似值）') : null))
      : h('div.notice.notice-warn.mt-2', h('div.small', st.why)),
    ...warn.map((w) => h('div.notice.notice-warn.mt-2', h('div.small', w))),
    h('p.xsmall.dim.mt-2.measure',
      '皮尔逊 r 看的是「像不像一条直线」，斯皮尔曼 ρ 只看「是不是一个涨另一个也涨」'
      + '（排名的相关，不怕个别极端值）。',
      '两者都在 −1 到 1 之间，越接近 ±1 越强。'
      + 'p 是「两者其实无关、只是碰巧」时看到这么强相关的概率，',
      '常用的门槛是 0.05。虚线是最小二乘直线，只是帮眼睛看趋势。',
      h('strong', '相关不等于因果'),
      ' —— 批次之间的差别（比如前驱体换了一瓶）也会让两个量一起变。'));
}

/** 审核改了当前值 —— 散点、统计、箱线图都可能变，安静地重取一遍。 */
const refreshAfterReview = () => { loadPairs({ quiet: true }); loadGroups({ quiet: true }); };

/** 图上每个点对应哪个样品。也是键盘用户打开样品的路；一个坏数在这里就能作废。 */
function pointTable(d) {
  if (!d.points.length) return null;
  const review = (id, quality, label) => qualityControl({ id, quality }, { label, onChanged: refreshAfterReview });
  return h('details.mt-3', { open: S.tableOpen, ontoggle: (e) => { S.tableOpen = e.target.open; } },
    h('summary.small', `逐个样品（${fmtInt(d.points.length)}）`),
    h('div.panel.panel-body.flush.mt-2',
      h('p.xsmall.dim', { style: { padding: '6px 12px 0' } },
        '作废一个数，这个样品的这个字段就退回上一次没作废的结果；'
        + '一次都没有了，这个点就从图上拿掉。'),
      h('table.data',
        h('thead', h('tr', h('th', '样品'), h('th', '批次'),
          h('th.num', axisLabel(d.x)), h('th', 'X 审核'), h('th.num', axisLabel(d.y)), h('th', 'Y 审核'))),
        h('tbody', ...d.points.map((p) => h('tr',
          h('td', h('button.linklike', { onclick: () => refs.nav('sample', { arg: p.sample_id }) }, p.name)),
          h('td.small.muted', p.batch || '—'),
          h('td.num', fmt(p.x)), h('td', review(p.x_id, p.x_quality, `${p.name} 的 ${d.x.label || d.x.field_name}`)),
          h('td.num', fmt(p.y)), h('td', review(p.y_id, p.y_quality, `${p.name} 的 ${d.y.label || d.y.field_name}`))))))));
}
const fmt = (v) => (Math.abs(v) >= 1e5 || (Math.abs(v) < 1e-3 && v !== 0) ? v.toExponential(3) : Number(v.toPrecision(5)).toString());

// ------------------------------------------------------------------ 按批次的箱线图
async function loadGroups({ quiet = false } = {}) {
  const host = refs.groupsHost;
  const fresh = groupsTicket();
  if (!quiet) mount(host, skeletonRows(5));
  try {
    const d = await api.relationGroups({ y: S.g, by: 'batch', filter: scopeFilter() });
    if (!refs.alive() || !fresh()) return;
    S.groups = d;
    drawGroups();
  } catch (err) {
    if (!refs.alive() || !fresh()) return;
    mount(host, errorBox(err, loadGroups));
  }
}

function drawGroups() {
  const d = S.groups;
  const chart = boxPlot({ groups: d.groups, y_label: axisLabel(d.y), x_label: '批次' },
                        { onPick: (o) => refs.nav('sample', { arg: o.sample_id }) });
  const outliers = d.groups.flatMap((g) => g.outliers.map((o) => ({ ...o, group: g.name })));
  const dl = downloadMenu({
    svg: () => chart.querySelector('svg'),
    csv: () => rowsToCsv(['批次', '样品数', '最小', 'Q1', '中位数', 'Q3', '最大', '离群点'],
      d.groups.map((g) => [g.name, g.n, g.min, g.q1, g.median, g.q3, g.max,
                           g.outliers.map((o) => o.name).join(' ')])),
    name: `构效关系_${d.y.field_name}_按批次`,
  });
  mount(refs.groupsHost,
    figure(`${axisLabel(d.y)}  按批次`, {
      head: dl,
      ctl: [selectControl('字段', S.g, fieldOptions(), (v) => { S.g = v; remember(); loadGroups(); })],
      body: chart,
      note: h('div.relation-stats',
        h('div.small', `${fmtInt(d.n)} 个样品、${fmtInt(d.groups.length)} 个批次。`,
          '箱 = 中间一半样品（Q1–Q3），粗线 = 中位数，须 = 1.5 倍箱长以内最远的样品，'
          + '须外的点单独画出来。'),
        d.y.mixed_units ? h('div.notice.notice-warn.mt-2', h('div.small',
          `这个字段在不同样品上单位不一致（${d.y.units.join(' / ')}），箱的位置不可信`)) : null,
        outliers.length
          ? h('div.small.mt-2', h('div', '离群的样品（离群不等于错 —— 看过之后可以在这里直接审核）：'),
              h('div.outlier-list', ...outliers.map((o) => h('div.row.gap-2', { style: { alignItems: 'center' } },
                h('button.linklike', { onclick: () => refs.nav('sample', { arg: o.sample_id }) },
                  `${o.name}（${o.group}）`),
                h('span.dim.mono', fmt(o.y)),
                o.result_id ? qualityControl({ id: o.result_id, quality: o.quality },
                  { label: `${o.name} 的 ${d.y.label || d.y.field_name}`, onChanged: refreshAfterReview }) : null))))
          : null),
    }));
}

// ------------------------------------------------------------------ 字段清单 + 还没做的
function fieldsSection() {
  return h('div.section',
    h('div.section-head',
      h('div.section-title', '全部可分析的字段'),
      h('div.section-note', `${S.fields.length} 个数值字段`)),
    h('details',
      h('summary.small', '展开'),
      h('div.panel.panel-body.flush.mt-2',
        fieldTable(S.fields, { numeric: true }))));
}

function laterSection() {
  const rows = [
    ['已做', 'X / Y 任选两个字段做散点，按批次着色'],
    ['已做', '相关性：皮尔逊 / 斯皮尔曼，带 p 值和置信区间'],
    ['已做', '按批次分组的箱线图，离群样品点得进去'],
    ['已做', '审核：逐个样品的表和离群点上直接标「可信 / 待查 / 作废」，作废的数不再进图'],
    ['还没做', '异常识别：把偏离趋势的样品挑出来，回溯到原始文件'],
    ['还没做', '总结：把上面这些结论交给模型写成一段可读的说明'],
  ];
  return h('div.section',
    h('div.section-head', h('div.section-title', '这一层的进度')),
    h('div.panel.panel-body.flush',
      ...rows.map(([st, d], i, arr) => h('div', {
        style: { padding: 'var(--s3x) var(--s4x)',
                 borderBottom: i === arr.length - 1 ? '0' : '1px solid var(--line)' },
      }, h('div.row.gap-3',
        h(`span.status.small.${st === '已做' ? 'status-ok' : 'status-idle'}`, st),
        h('span.small.muted', d))))));
}
