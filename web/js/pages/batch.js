// 批处理结果页：叠图 + 运行状态表 + 导出。
//
// 上千条曲线叠一张图是噪声不是图 —— 超过阈值自动降级成分位数带，
// 并在图注里写明降级了。不偷偷少画。

import { api } from '../api.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy,
  fmtInt, fmtNum, fmtTime, modal, sampleLabel,
} from '../ui.js';
import { xyChart, quantileBand, seriesColor } from '../chart.js';

export const meta = {
  id: 'batch',
  parent: 'process',
  title: '批处理结果',
  desc: '',
};

// 超过这么多条就降级成分位数带。可以手动切回去。
const SPAGHETTI_LIMIT = 60;

const S = {
  runId: null, detail: null, curves: null,
  column: 'integral', mode: 'auto', groupBy: 'batch', error: null,
};

let refs = {};

export function actions(nav) {
  return [h('button.btn.btn-sm', { onclick: () => nav('process') }, '← 返回样品列表')];
}

export async function view(host, ctx) {
  S.runId = ctx.arg;
  S.detail = S.curves = S.error = null;
  refs = { host, nav: ctx.nav };

  if (!S.runId) {
    mount(host, empty('没有指定批处理', h('button.btn.btn-sm',
      { onclick: () => ctx.nav('process') }, '回到样品列表')));
    return;
  }

  mount(host, skeletonRows(8));
  try {
    S.detail = await api.batchDetail(S.runId);
  } catch (err) {
    mount(host, errorBox(err, () => view(host, ctx)));
    return;
  }

  const params = S.detail.run.params || {};
  document.querySelector('#pageTitle').textContent = '批处理结果';
  document.querySelector('#pageDesc').textContent =
    `${fmtInt(params.n_samples || S.detail.children.length)} 个样品 · `
    + fmtTime(S.detail.run.started_at);

  const chartHost = h('div#chartHost');
  const tableHost = h('div#tableHost');
  mount(host, header(), h('div.section', chartHost), h('div.section', tableHost));
  refs.chartHost = chartHost;
  refs.tableHost = tableHost;

  drawChart();
  drawTable();
  loadCurves();
}

function header() {
  const d = S.detail;
  const params = d.run.params || {};
  const recipe = params.recipe || {};
  return h('div.section',
    h('div.metrics.metrics-4',
      metric('样品', d.children.length, '本次处理'),
      metric('成功', d.n_ok, d.n_ok === d.children.length ? '全部通过' : ''),
      metric('失败', d.n_failed, d.n_failed ? '见下表' : '无'),
      metric('曲线表', d.tables.length ? d.tables[0].n_rows : 0, '长表行数')),
    h('div.mt-4.row.wrap.gap-3',
      h('span.small.muted', '配方：'),
      ...Object.entries(recipe).filter(([, v]) => v !== null && v !== 0)
        .map(([k, v]) => h('span.tag', `${RECIPE_LABELS[k] || k} ${fmtNum(v)}`))));
}

const RECIPE_LABELS = {
  band_min: '膜厚窗口起', band_max: '膜厚窗口止',
  integral_min: '积分起', integral_max: '积分止',
  slope_center: '斜率波长', slope_half_width: '斜率半宽',
};

const metric = (label, value, note) => h('div.metric',
  h('div.metric-label', label),
  h('div.metric-value' + (value ? '' : '.is-zero'), fmtInt(value)),
  note ? h('div.metric-note', note) : null);

// ------------------------------------------------------------------ 叠图
async function loadCurves() {
  try {
    S.curves = await api.batchCurves(S.runId, { column: S.column, max_series: 1200 });
  } catch (err) {
    S.error = err;
  }
  drawChart();
}

/** 屏幕上现在画的是叠图还是分位数带。导出必须跟它一致。 */
function effectiveMode() {
  const n = S.curves?.series?.length || 0;
  return (S.mode === 'band' || (S.mode === 'auto' && n > SPAGHETTI_LIMIT)) ? 'band' : 'overlay';
}

function drawChart() {
  const host = refs.chartHost;
  if (!host) return;
  if (S.error) { mount(host, errorBox(S.error, loadCurves)); return; }
  if (!S.curves) { mount(host, skeletonRows(5)); return; }

  const all = S.curves.series;
  const n = all.length;
  const degrade = effectiveMode() === 'band';

  let chart, caption;
  if (degrade) {
    const band = quantileBand(all);
    // 带里再画几条代表曲线，让人看到个体形状而不只是统计量
    const step = Math.max(1, Math.floor(n / 6));
    const reps = all.filter((_, i) => i % step === 0).slice(0, 6)
      .map((s) => ({ ...s, group: undefined }));
    chart = xyChart({
      x_label: '时间 (s)', y_label: Y_LABEL[S.column], band,
      series: reps.map((s) => ({ ...s, style: 'line' })),
    }, { height: 340 });
    caption = `${fmtInt(n)} 条曲线 —— 显示中位数与四分位区间，另叠了 ${reps.length} 条代表曲线。`
      + `超过 ${SPAGHETTI_LIMIT} 条自动降级，因为上千条叠一起是噪声不是图。`;
  } else {
    chart = xyChart({
      x_label: '时间 (s)', y_label: Y_LABEL[S.column],
      series: all.map((s) => ({ ...s, style: 'line',
        group: S.groupBy === 'batch' ? s.group : undefined })),
    }, { height: 340 });
    caption = `${fmtInt(n)} 条曲线`
      + (S.groupBy === 'batch' ? '，按批次着色（最多 12 组）' : '');
  }

  if (S.curves.truncated) {
    caption += `　服务端只返回了前 ${fmtInt(S.curves.returned)} 条，`
      + `另有 ${fmtInt(S.curves.truncated)} 条没取。`;
  }

  mount(host,
    h('div.section-head',
      h('div.section-title', '跨样品叠图'),
      h('div.row.gap-3',
        select('曲线', S.column, [['integral', '波段积分'], ['slope', '谱斜率']],
          (v) => { S.column = v; S.curves = null; drawChart(); loadCurves(); }),
        select('显示', S.mode,
          [['auto', `自动（>${SPAGHETTI_LIMIT} 条降级）`], ['all', '全部画出'],
           ['band', '强制分位数带']],
          (v) => { S.mode = v; drawChart(); }),
        select('着色', S.groupBy, [['batch', '按批次'], ['none', '逐条']],
          (v) => { S.groupBy = v; drawChart(); }),
        h('button.btn.btn-sm', { onclick: () => exportDialog() }, '导出脚本'))),
    chart,
    h('div.chart-caption', caption));
}

const Y_LABEL = { integral: '积分强度 (a.u.·nm)', slope: 'dI/dλ (a.u./nm)' };

function select(label, value, options, onChange) {
  return h('label.inline-field',
    h('span.small.muted', label),
    h('select.select.select-sm', { onchange: (e) => onChange(e.target.value) },
      ...options.map(([v, t]) => h('option', { value: v, selected: v === value }, t))));
}

// ------------------------------------------------------------------ 状态表
function drawTable() {
  const host = refs.tableHost;
  if (!host) return;
  const d = S.detail;
  const failed = d.children.filter((c) => c.status === 'failed');
  const warned = d.children.filter((c) => c.status === 'ok' && c.warnings?.length);

  mount(host,
    h('div.section-head',
      h('div.section-title', '运行状态'),
      h('div.section-note',
        d.n_failed ? `${fmtInt(d.n_failed)} 个失败` : '全部成功',
        warned.length ? ` · ${fmtInt(warned.length)} 个有警告` : '')),
    failed.length ? failedTable(failed) : null,
    warned.length ? warningBlock(warned) : null,
    allTable(d.children));
}

/** 失败的样品单独列在最上面 —— 跑 1000 个样品时这张表比图重要。 */
function failedTable(failed) {
  return h('div.panel.panel-body.flush.mb-3',
    h('table.data',
      h('thead', h('tr', h('th', '样品'), h('th', '批次'), h('th', '失败原因'))),
      h('tbody', ...failed.map((c) => h('tr',
        h('td.strong', sampleLabel(c.sample_name, c.batch)),
        h('td.small.muted', c.batch || '—'),
        h('td.small', { style: { color: 'var(--danger)' } },
          String(c.error || '').split('\n')[0]))))));
}

function warningBlock(warned) {
  const rows = warned.slice(0, 50).map((c) => h('tr',
    h('td.strong', sampleLabel(c.sample_name, c.batch)),
    h('td.small.muted', (c.warnings || []).join('；'))));
  return h('details.mb-3',
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      `${fmtInt(warned.length)} 个样品有警告`),
    h('div.panel.panel-body.flush.mt-2',
      h('table.data', h('tbody', ...rows))));
}

function allTable(children) {
  const rows = children.map((c) => h('tr',
    h('td', sampleLabel(c.sample_name, c.batch)),
    h('td.small.muted', c.batch || '—'),
    h('td', h('span.status.' + (c.status === 'ok' ? 'status-ok' : 'status-danger'),
              c.status === 'ok' ? '成功' : '失败')),
    h('td.num', fmtInt(c.n_results))));
  return h('details',
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      `全部 ${fmtInt(children.length)} 个样品`),
    h('div.panel.panel-body.flush.mt-2',
      { style: { maxHeight: '420px', overflow: 'auto' } },
      h('table.data',
        h('thead', h('tr', h('th', '样品'), h('th', '批次'), h('th', '状态'),
                     h('th.num', '结果数'))),
        h('tbody', ...rows))));
}

// ------------------------------------------------------------------ 导出
function exportDialog() {
  // 「自动」模式下屏幕已经降级成分位数带了，导出就不能还是 90 条叠图 ——
  // 拿到手的图跟屏幕上看到的不是同一张，是最让人困惑的一种不一致。
  const mode = effectiveMode();
  const url = `/api/batch/runs/${S.runId}/export?column=${S.column}`
    + `&mode=${mode}&group_by=${S.groupBy}`;
  const m = modal({
    title: '导出绘图脚本',
    width: '640px',
    body: h('div',
      h('p.small',
        '导出一个 zip：', h('span.mono', 'plot.py'), ' + ', h('span.mono', 'data.csv'),
        ' + ', h('span.mono', 'README.md'), '。'),
      h('p.small.muted.mt-3',
        '脚本按你的 matplotlib 规范生成 —— rcParams、12 色序列、标记、线型都在里面，',
        '解压出来直接 ', h('span.mono', 'python plot.py'), ' 就能出 300 dpi 的图。'),
      h('div.notice.notice-accent.mt-4',
        h('div.grow',
          h('div.small.strong', '为什么是脚本而不是让模型直接画'),
          h('p.xsmall.dim.mt-2',
            '平台内置图型不够用时你拿脚本走人随便改；',
            '而且论文里那张图是你自己能读、能改、能引用的代码画的 —— ',
            '不是模型在某个沙箱里跑出来的黑箱。'))),
      h('p.xsmall.dim.mt-3',
        '当前导出：', h('span.mono', `${Y_LABEL[S.column]} · `
          + (mode === 'band' ? '分位数带' : '叠图')
          + ` · 按${S.groupBy === 'batch' ? '批次' : '逐条'}着色`),
        mode === 'band' && S.mode === 'auto'
          ? '（跟图上一样，因为曲线超过阈值已经降级）' : null)),
    foot: [
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('a.btn.btn-primary', { href: url, download: '' }, '下载 zip'),
    ],
  });
}
