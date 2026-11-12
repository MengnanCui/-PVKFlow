// QFLS 单点详情：#qfls-point/<处理编号>/<样点 key>。
// 身份、分数怎么来的、异常、全部指标（带组内 z 和组均值）、六张图、qvdf 元数据；
// 上一个 / 下一个走当前筛选的顺序。

import { api } from '../api.js';
import { h, mount, render, toast, empty } from '../ui.js';
import { figure } from '../components/figure.js';
import { S, openRun, filtered, fmtMetric, unitOf, FLAG_TEXT, toggleCompare, COMPARE_MAX } from './qfls-state.js';
import { gradeChip, limits } from './qfls-views.js';

export const meta = {
  id: 'qfls-point',
  title: 'QFLS 样点',
  desc: '一个样点的全部指标、分数来由、异常和图',
  parent: 'qfls',
  parentArg: 'rank',     // 直接打开时「返回」回排名表；从别处点进来回别处（app.js，C077）
};

export function view(host, ctx) {
  const [runId, key] = String(ctx.arg || '').split('/');
  if (!runId || !key) {
    mount(host, empty('没有指定样点',
      h('button.btn.btn-sm', { onclick: () => ctx.nav('qfls', { arg: 'rank' }) }, '去排名表挑一个')));
    return null;
  }
  const alive = ctx.alive || (() => true);
  return render(host, async () => {
    if (S.runId !== runId || !S.points) await openRun(runId);
    return api.qflsPoint(runId, key);
  }, (d) => {
    // 标题写样点编号（和样品页、批处理页一样，标题栏说的是「现在看的是哪一个」）
    if (alive()) document.querySelector('#pageTitle').textContent = `QFLS 样点 · ${d.point.code}`;
    return page(ctx, runId, d);
  });
}

const fact = (k, v) => h('div', h('div.fact-k', k), h('div.fact-v', v ?? '—'));

function page(ctx, runId, d) {
  const p = d.point;
  const order = filtered().map((x) => x.key);
  const i = order.indexOf(p.key);
  const go = (k) => ctx.nav('qfls-point', { arg: `${runId}/${k}` });
  const gs = S.summary?.group_stats?.[`${p.experiment}|${p.objective || '?'}`] || {};
  const img = (name, title, note) => (d.images.includes(name)
    ? figure(title, {
        body: h('a.qfls-img',
          { href: api.qflsFile(runId, `points/${p.key}/${name}`), target: '_blank', rel: 'noopener', title: '新窗口打开原图' },
          h('img', { src: api.qflsFile(runId, `points/${p.key}/${name}`), alt: `${p.code} ${title}`, loading: 'lazy' })),
        note: note ? h('div.chart-caption', note) : null })
    : null);
  const inCmp = S.compare.includes(p.key);
  const cmpBtn = h('button.btn.btn-sm', {
    disabled: !p.valid,
    onclick: () => {
      if (!toggleCompare(p.key)) { toast(`最多对比 ${COMPARE_MAX} 个`, 'warn'); return; }
      toast(S.compare.includes(p.key) ? `加进对比了（${S.compare.length} 个）` : '从对比里去掉了', 'ok');
      cmpBtn.textContent = S.compare.includes(p.key) ? '从对比里去掉' : '加入对比';
    },
  }, inCmp ? '从对比里去掉' : '加入对比');

  return h('div',
    h('div.row.gap-2.wrap',
      h('button.btn.btn-sm', { disabled: i <= 0, onclick: () => go(order[i - 1]) }, '上一个'),
      h('button.btn.btn-sm', { disabled: i < 0 || i >= order.length - 1, onclick: () => go(order[i + 1]) }, '下一个'),
      h('span.small.muted', i >= 0 ? `当前筛选里第 ${i + 1} / ${order.length} 个` : '不在当前筛选里'),
      h('span.grow'), cmpBtn,
      h('button.btn.btn-sm.btn-ghost', { onclick: () => ctx.nav('qfls', { arg: 'compare' }) }, `去对比（${S.compare.length}）`)),

    h('div.qfls-head.mt-4',
      h('div', h('div.mono.strong.qfls-code', p.code), h('div.small.muted', `${p.experiment} · ${p.batch} · ${p.slot} · ${p.point}`)),
      h('div.qfls-score', gradeChip(p.grade),
        p.valid ? h('div', h('div.metric-value', p.score.toFixed(1)), h('div.xsmall.muted',
          `全局第 ${p.rank} · ${p.objective} 内第 ${p.rank_in_objective}`)) : h('div.small.danger', '无效帧，不打分'))),

    p.flags.length ? h('div.mt-3', ...p.flags.map((f) => h(
      `div.notice.qfls-flag.${f.level === 'error' ? 'notice-danger' : f.level === 'warn' ? 'notice-warn' : ''}`,
      h('div.grow', h('span.strong', FLAG_TEXT[f.code] || f.code), ' · ', f.text))))
      : h('div.notice.mt-3', h('div.grow', '没有任何异常标记')),

    h('div.facts.qfls-facts.mt-4',
      fact('物镜', p.objective), fact('激光', p.laser), fact('光强', p.intensity),
      fact('采集时间', p.timestamp?.replace('T', ' ')),
      fact('矩阵', p.rows ? `${p.rows} × ${p.cols}` : null),
      fact('像素尺寸', p.pixel_size_um ? `${p.pixel_size_um} µm` : '不知道（qvdf 里没有）'),
      fact('qvdf 帧数', p.meta?.frames), fact('uuid', h('span.mono.xsmall', p.uuid || '—'))),
    h('div.xsmall.muted.mono.mt-2', p.rel),

    p.valid ? null : h('div.notice.notice-danger.mt-4', h('div.grow', p.error)),

    p.valid ? h('div.section.mt-6',
      h('div.section-head', h('div.section-title', '图'),
        d.images.includes('summary.png')
          ? h('a.btn.btn-sm.btn-ghost', { href: api.qflsFile(runId, `points/${p.key}/summary.png`), target: '_blank' }, '汇总图')
          : null),
      h('div.qfls-grid',
        img('heatmap.png', '热图', '色标 = 这一帧自己的 P1 – P99'),
        img('anomaly.png', '异常图',
          '蓝 = 低值连通域，红 = 划痕候选，橙 = 亮点；灰虚线 = 边缘带内沿，绿点线 = 中心区'),
        img('hist.png', '直方图', '红实线 = 中位数，虚线 = 中位数 ± 2σ'),
        img('profile.png', '剖面图', '中间 5% 行 / 列的平均'),
        img('variogram.png', '变异函数', '灰虚线 = 基台（方差），红点划线 = 变程'))) : null,

    p.valid ? h('div.section',
      h('div.section-head', h('div.section-title', '综合分怎么来的'), h('div.section-note', '100 减下面各项')),
      h('div.panel.panel-body.flush.table-wrap', h('table.data',
        h('thead', h('tr', h('th', '扣分项'), h('th.num', '扣了'), h('th.num', '最多'))),
        h('tbody', ...p.penalties.map((x) => h('tr' + (x.points ? '' : '.dim'), h('td', x.label),
          h('td.num', x.points.toFixed(1)), h('td.num', String(x.max)))),
          h('tr', h('td.strong', '综合分'), h('td.num.strong', p.score.toFixed(1)), h('td')))))) : null,

    p.valid ? h('div.section',
      h('div.section-head', h('div.section-title', '全部指标'),
        h('div.section-note', `组 = ${p.experiment} × ${p.objective}；组内 z = (x − 组均值) / 组标准差`)),
      limits(),
      h('div.panel.panel-body.flush.table-wrap.mt-3', h('table.data',
        h('thead', h('tr', h('th', '指标'), h('th.num', '值'), h('th', '单位'), h('th.num', '组均值'),
          h('th.num', '组内 z'), h('th', '跨物镜'))),
        h('tbody', ...S.summary.metrics.map((m) => {
          const z = p.z?.[m.key];
          return h('tr', h('td', m.label), h('td.num.strong', fmtMetric(m.key, p[m.key])),
            h('td.xsmall.muted', unitOf(m.key) || (/_frac$/.test(m.key) ? '%' : '')),
            h('td.num.small', fmtMetric(m.key, gs[m.key]?.mean)),
            h('td.num' + (z != null && Math.abs(z) >= 2 ? '.qfls-z-hi' : ''), z == null ? '—' : z.toFixed(2)),
            h('td.xsmall', m.cross_objective ? '能' : h('span.warn-text', '不能')));
        }),
        h('tr', h('td', '最大低值连通域'), h('td.num', `${fmtMetric('low_cc_max_px', p.low_cc_max_px)} 像素`),
          h('td.xsmall.muted', p.low_cc_max_um2 ? `${p.low_cc_max_um2.toFixed(1)} µm²` : ''), h('td'), h('td'),
          h('td.xsmall', h('span.warn-text', '不能'))),
        h('tr', h('td', '变程（µm）'),
          h('td.num', p.variogram_range_um == null ? '—' : p.variogram_range_um.toFixed(2)),
          h('td.xsmall.muted', 'µm'), h('td'), h('td'), h('td.xsmall', '能')),
        h('tr', h('td', 'P1 / P10 / P90 / P99'),
          h('td.num.small', [p.p1, p.p10, p.p90, p.p99].map((v) => v.toFixed(4)).join(' / ')),
          h('td.xsmall.muted', 'eV'), h('td'), h('td'), h('td'))))),
      p.scratches?.length ? h('div.small.mt-3', h('span.strong', '划痕候选：'), ...p.scratches.map((s, j) =>
        h('div.mono.xsmall', `#${j + 1} 长 ${s.length_px} 像素、宽 ${s.width_px}、长短轴比 ${s.elong}、`
          + `面积 ${s.area_px}、中心 (${s.cx}, ${s.cy})`))) : null) : null,

    Object.keys(p.meta || {}).length ? h('details.section',
      h('summary.small', `qvdf 元数据（${p.meta_format || '?'}，${p.qvdf_rel || '—'}）`),
      h('div.panel.panel-body.flush.table-wrap', h('table.data', h('tbody',
        ...Object.entries(p.meta).map(([k, v]) => h('tr', h('td.mono.xsmall', k),
          h('td.small', typeof v === 'object' ? JSON.stringify(v) : String(v)))))))) : null);
}
