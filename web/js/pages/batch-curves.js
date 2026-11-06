// 批处理结果页：曲线叠图，和导出（数据 + 一份能自己跑的 plot.py）。
//
// 上千条曲线叠一张图是噪声不是图 —— 超过阈值自动降级成分位数带，
// 并在图注里写明降级了。不偷偷少画。

import { api } from '../api.js';
import { infoDot, withInfo } from '../components/info.js';
import { h, mount, toast, skeletonRows, errorBox, busy, fmtInt, modal } from '../ui.js';
import { xyChart, quantileBand } from '../chart.js';
import { S, Y_LABEL, batchCount, refs, select, yLabel } from './batch-state.js';

// 超过这么多条就降级成分位数带。可以手动切回去。
const SPAGHETTI_LIMIT = 60;

// 按批次着色时，同一批次的几片样品**必然同色** —— 颜色这个维度已经被批次
// 用掉了。线型是第二个维度，把同批次内的样品分开。
// 没有它，三条曲线两个批次时图上就是「两条一模一样的线」，这正是你看到的。
const DASHES = [null, '7 3', '2 3', '9 3 2 3'];

/** 一张叠图的现状：几条曲线、有没有降级、怎么着色。 */
function curveSpot(col, title) {
  const data = S.curves[col];
  const nSeries = data?.series?.length || 0;
  return {
    title: `「${title}」这张图`,
    rows: {
      曲线条数: nSeries,
      当前画法: effectiveMode(col) === 'band'
        ? `分位数带（超过 ${SPAGHETTI_LIMIT} 条自动降级，只显示中位数和四分位区间）`
        : '每条都画出来',
      着色: S.groupBy === 'batch'
        ? `颜色分批次（${fmtInt(batchCount(data?.series || []))} 个），同批次内按线型分样品`
        : '逐条一色',
      纵轴: yLabel(col),
      服务端截断: data?.truncated ? `还有 ${data.truncated} 条没取` : '',
    },
    scope: { run_id: S.runId },
  };
}

// ------------------------------------------------------------------ 特殊处理对比
//
// 左右并排两张：谱斜率 vs 时间 | 波段积分 vs 时间。
// 对比看的就是这两条 —— 一次全画出来，不用先选一个再切另一个。
export async function loadCurves() {
  const alive = refs.alive;
  // 重试要先把上一次的错清掉。原来不清：点「重试」之后 drawChart 看见的还是那个错，
  // 永远画同一个报错（自查 §6 #22）
  S.error = null;
  drawChart();
  const cols = ['integral', 'slope', 'ot', ...(S.extra || []).map((c) => c.column)];
  await Promise.all(cols.map(async (col) => {
    let data;
    try {
      data = await api.batchCurves(S.runId, { column: col, max_series: 1200 });
      if (!alive()) return;
      S.curves[col] = data;
    } catch (err) {
      if (!alive()) return;
      // 膜厚可能整个缺席（老的对比跑在加膜厚之前）；同事模块的一列出问题也一样 ——
      // 都不该把别的图拖垮，缺的那一张自己显示原因就行。
      if (col === 'integral' || col === 'slope') S.error = err;
      else S.curves[col] = { error: err };
    }
    drawChart();
  }));
}

/** 屏幕上现在画的是叠图还是分位数带。导出必须跟它一致。 */
function effectiveMode(col) {
  const n = S.curves[col]?.series?.length || 0;
  return (S.mode === 'band' || (S.mode === 'auto' && n > SPAGHETTI_LIMIT)) ? 'band' : 'overlay';
}

export function drawChart() {
  const host = refs.chartHost;
  if (!host) return;
  if (S.error) { mount(host, errorBox(S.error, loadCurves)); return; }

  mount(host,
    h('div.section-head',
      h('div.section-title', '特殊处理对比'),
      h('div.row.gap-3.wrap',
        h('span.row.gap-1',
          select('显示', S.mode,
            [['auto', `自动（>${SPAGHETTI_LIMIT} 条降级）`], ['all', '全部画出'],
             ['band', '强制分位数带']],
            (v) => { S.mode = v; drawChart(); }),
          // 「为什么我这张图变成了分位数带」—— 答案是当前条数和阈值，
          // 那两个数就在这儿。三张图的现状一起带上，省得再问一遍。
          infoDot('quantile_band', {
            spot: () => ({
              title: '「特殊处理对比」这三张叠图',
              rows: ['ot', 'slope', 'integral'].map((c) => [
                Y_LABEL[c],
                `${fmtInt(S.curves[c]?.series?.length || 0)} 条`,
                effectiveMode(c) === 'band' ? '已降级成分位数带' : '每条都画出来',
              ]),
              note: `当前显示模式：${S.mode === 'auto' ? `自动（超过 ${SPAGHETTI_LIMIT} 条降级）`
                : S.mode === 'band' ? '强制分位数带' : '全部画出'}`,
              scope: { run_id: S.runId },
            }),
          })),
        select('着色', S.groupBy, [['batch', '按批次'], ['none', '逐条']],
          (v) => { S.groupBy = v; drawChart(); }))),
    h('div.fig-grid-2',
      curveFigure('ot', '光学厚度 vs 时间'),
      curveFigure('slope', '谱斜率 vs 时间')),
    ...curveRows([
      curveFigure('integral', '波段积分 vs 时间'),
      ...(S.extra || []).map((c) => curveFigure(c.column,
        c.module_name ? `${c.label}（${c.module_name}）` : c.label)),
    ]));
}

/** 两张一排。单出来的那张旁边放一格空的，保持左右对齐。 */
function curveRows(cells) {
  if (cells.length % 2) cells.push(h('div.figure'));
  const rows = [];
  for (let i = 0; i < cells.length; i += 2) {
    rows.push(h('div.fig-grid-2.mt-6', cells[i], cells[i + 1]));
  }
  return rows;
}

// 和样品页一样的三行结构：标题 / 功能 / 图。没有功能的那一格第二行空着，
// 但仍然占着 subgrid 的那一行 —— 左右两张图才会从同一条线开始。
function curveFigure(col, title) {
  const data = S.curves[col];
  const body = data?.error
    ? h('div.notice.notice-warn',
        h('div.grow',
          h('div.small', data.error.message),
          h('p.xsmall.dim.mt-2', '用上面的参数重跑一次就有了。')))
    : data ? paintCurve(col, data) : skeletonRows(4);
  return h('div.figure',
    h('div.figure-head', h('div.figure-title', COL_INFO[col]
      ? withInfo(title, COL_INFO[col], { spot: () => curveSpot(col, title) }) : title)),
    h('div.figure-ctl', data && !data.error
      ? h('button.btn.btn-sm', { onclick: () => exportDialog(col) }, '导出脚本')
      : null),
    h('div.figure-body', body));
}

const COL_INFO = { ot: 'ot', slope: 'slope', integral: 'integral' };

function paintCurve(col, data) {
  const all = data.series;
  const n = all.length;
  const degrade = effectiveMode(col) === 'band';

  let chart, caption;
  if (degrade) {
    const band = quantileBand(all);
    // 带里再画几条代表曲线，让人看到个体形状而不只是统计量
    const step = Math.max(1, Math.floor(n / 6));
    const reps = all.filter((_, i) => i % step === 0).slice(0, 6)
      .map((s) => ({ ...s, group: undefined }));
    chart = xyChart({
      x_label: '时间 (s)', y_label: yLabel(col), band,
      series: reps.map((s) => ({ ...s, style: 'line' })),
    }, { height: 300 });
    caption = `${fmtInt(n)} 条曲线 —— 显示中位数与四分位区间，另叠了 ${reps.length} 条代表曲线。`
      + `超过 ${SPAGHETTI_LIMIT} 条自动降级，因为上千条叠一起是噪声不是图。`;
  } else {
    chart = xyChart({
      x_label: '时间 (s)', y_label: yLabel(col),
      series: styled(all),
    }, { height: 300 });
    caption = `${fmtInt(n)} 条曲线`
      + (S.groupBy === 'batch'
          ? `，颜色分批次（${fmtInt(batchCount(all))} 个），同批次内按线型分样品`
          : '，逐条一色');
  }

  if (data.truncated) {
    caption += `　服务端只返回了前 ${fmtInt(data.returned)} 条，`
      + `另有 ${fmtInt(data.truncated)} 条没取。`;
  }
  return h('div', chart, h('div.chart-caption', caption));
}

/**
 * 给每条曲线定颜色维度和线型维度。
 *
 * 按批次着色时，`group` 交给 chart.js 去分配颜色（它对齐 matplotlib 的 12 色），
 * 线型在这里按「同批次内第几条」分配。两个维度合起来才能让 ZG0014 的两片样品
 * 在图上分得开 —— 只给颜色的话它俩是同一条线。
 */
function styled(series) {
  const seen = new Map();
  return series.map((s) => {
    if (S.groupBy !== 'batch') return { ...s, style: 'line', group: undefined, dash: null };
    const g = s.group || '';
    const n = seen.get(g) || 0;
    seen.set(g, n + 1);
    return { ...s, style: 'line', group: s.group, dash: DASHES[n % DASHES.length] };
  });
}

// ------------------------------------------------------------------ 导出
/** 下载之前先把脚本读一眼 —— 论文里那张图的来源，值得先看看它画的是什么。 */
async function previewScript(params) {
  try {
    const r = await api.batchExportPreview(S.runId, params);
    modal({
      title: 'plot.py',
      width: '860px',
      body: h('div',
        h('p.xsmall.dim',
          `${fmtInt(r.n_series)} 条曲线 · data.csv ${fmtInt(r.n_rows)} 行 · `,
          h('span.mono', r.columns.join(', '))),
        h('pre.code-block.mt-3', r.script)),
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}

function exportDialog(col) {
  // 「自动」模式下屏幕已经降级成分位数带了，导出就不能还是 90 条叠图 ——
  // 拿到手的图跟屏幕上看到的不是同一张，是最让人困惑的一种不一致。
  const mode = effectiveMode(col);
  const params = { column: col, mode, group_by: S.groupBy };
  const url = api.batchExportUrl(S.runId, params);
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
        '当前导出：', h('span.mono', `${yLabel(col)} · `
          + (mode === 'band' ? '分位数带' : '叠图')
          + ` · 按${S.groupBy === 'batch' ? '批次' : '逐条'}着色`),
        S.groupBy === 'batch'
          ? h('div.xsmall.dim.mt-1',
              '导出脚本按批次分色，同批次内按 matplotlib 规范的线型循环区分样品 —— '
              + '和屏幕上这张一致。')
          : null,
        mode === 'band' && S.mode === 'auto'
          ? '（跟图上一样，因为曲线超过阈值已经降级）' : null)),
    foot: [
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('button.btn', {
        onclick: (e) => { busy(e.target, true); previewScript(params)
          .finally(() => busy(e.target, false)); },
      }, '先读一眼 plot.py'),
      // 没有后端就打不出 zip（静态演示版）。这时候不放一个点了没反应的按钮，
      // 直接说清楚 —— 脚本本身还是真的，读得到。
      url
        ? h('a.btn.btn-primary', { href: url, download: '' }, '下载 zip')
        : h('span.xsmall.dim', '这个版本没有后端，打不出 zip'),
    ],
  });
}
