// QFLS 成像的三个标签：总览 / 排名与筛选 / 多点对比。数据全来自 qfls-state.js 里已经取好的那次处理。

import { api } from '../api.js';
import { h, mount, empty, errorBox, skeletonRows, toast } from '../ui.js';
import { xyChart } from '../chart.js';
import { figure, selectControl } from '../components/figure.js';
import { boxPlot } from '../components/relation-charts.js';
import { downloadMenu } from '../download.js';
import { rowsToCsv } from '../csv.js';
import {
  S, filtered, metricDefs, FLAG_TEXT, BETTER, fmtMetric, unitOf, boxStats, toggleCompare, setCompare, COMPARE_MAX,
} from './qfls-state.js';

const GRADES = ['A', 'B', 'C', 'D'];
export const gradeChip = (g) => h(`span.qfls-grade.g-${g || 'x'}`, g || '无效');

/** 不同物镜不能比、单光强不做 JV —— 每个标签顶上都摆着（P-58）。 */
export function limits(extra = null) {
  return h('div.notice.notice-warn.qfls-limits', h('div.grow',
    h('span.strong', '读之前：'),
    '不同物镜（20X / 100X）的绝对 QFLS 和面积占比不能直接比较 —— 比较只在同一物镜内做，或看组内 z；',
    '只有单一光强（1 sun），不做伪 JV、理想因子、PCE、FF。', extra));
}

/** 行可点：回车 / 空格打开；行里的复选框、按钮上的按键不冒泡成「打开」。 */
export function clickableRow(open) {
  return {
    tabindex: 0,
    onclick: open,
    onkeydown: (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    },
  };
}

/** 箱线图 / 表格里的数：占比用 %，eV 差值用 meV。 */
function scaled(key) {
  if (/_frac$/.test(key)) return [100, '%'];
  if (['std', 'p90_p10', 'p99_p1', 'edge_minus_center'].includes(key)) return [1000, 'meV'];
  return [1, unitOf(key)];
}

// ================================================================== 总览
export function overviewTab(ctx) {
  const rows = filtered();
  const valid = rows.filter((p) => p.valid);
  const objs = [...new Set(valid.map((p) => p.objective || '?'))].sort();
  const defs = metricDefs();
  const nA = valid.filter((p) => p.grade === 'A').length;
  const host = h('div');

  const kpi = (label, value, note) => h('div.metric', h('div.metric-label', label),
    h('div.metric-value' + (value ? '' : '.is-zero'), String(value)), h('div.metric-note', note));

  const boxHost = h('div.qfls-grid');
  const drawBoxes = () => {
    const [mul, unit] = scaled(S.metric);
    const d = defs[S.metric];
    mount(boxHost, ...objs.map((o) => {
      const exps = S.summary.experiments.filter((e) => valid.some((p) => p.experiment === e.name && (p.objective || '?') === o));
      const pts = valid.filter((p) => (p.objective || '?') === o && p[S.metric] != null);
      const groups = exps.map((e) => boxStats(`${e.code} ${e.name}`,
        pts.filter((p) => p.experiment === e.name).map((p) => ({ y: p[S.metric] * mul, name: p.code, sample_id: p.key }))));
      const yLabel = `${d.label}${unit ? ` (${unit})` : ''}`;
      const chart = boxPlot({ groups, y_label: yLabel, x_label: '实验' },
                            { height: 260, onPick: (pt) => ctx.openPoint(pt.sample_id) });
      return figure(`${d.label} · ${o}`, {
        head: downloadMenu({
          svg: () => chart.querySelector('svg'), name: `QFLS_${S.metric}_${o}`,
          csv: () => rowsToCsv(['样点', '实验', '物镜', yLabel], pts.map((p) => [p.code, p.experiment, o, p[S.metric] * mul])),
        }),
        body: chart,
        note: h('div.chart-caption', d.cross_objective
          ? '这个指标是相对量，不同物镜之间可以比，这里仍按物镜分开画。'
          : '绝对量：只在同一物镜内比较。', ' 点离群点打开那个样点。'),
      });
    }));
  };
  drawBoxes();

  mount(host,
    limits(),
    h('div.metrics.metrics-4.mt-4',
      kpi('样点', rows.length, `${S.summary.n_frames} 帧里筛出的`),
      kpi('有效', valid.length, `${rows.length - valid.length} 帧数据无效、不打分`),
      kpi('有异常标记', rows.filter((p) => p.anomaly).length, 'warn / error 级'),
      kpi('A 级', nA, valid.length ? `占有效的 ${Math.round((100 * nA) / valid.length)}%` : '—')),

    h('div.section.mt-6',
      h('div.section-head', h('div.section-title', '等级分布（实验 × 物镜）'),
        h('div.section-note', '点一行只看这一组')),
      gradeTable(rows, ctx)),

    h('div.section',
      h('div.section-head', h('div.section-title', '指标分布'),
        selectControl('指标', S.metric, S.summary.metrics.map((m) => [m.key, m.label]),
          (v) => { S.metric = v; drawBoxes(); })),
      boxHost),

    h('div.section',
      h('div.section-head', h('div.section-title', '扫描报告'),
        h('div.section-note', `数据根目录 ${S.run.root}（只读）`)),
      scanReport()),

    h('div.section',
      h('div.section-head', h('div.section-title', '全局素材图'),
        h('div.section-note', '处理时按全部有效样点出的 PNG（不随筛选变），编号对照见扫描报告')),
      h('div.qfls-grid', ...['boxplots', 'grades', 'ranking'].map((n) =>
        h('a.qfls-img', { href: api.qflsFile(S.runId, `global/${n}.png`), target: '_blank', rel: 'noopener' },
          h('img', { src: api.qflsFile(S.runId, `global/${n}.png`), alt: `全局 ${n}`, loading: 'lazy' }))))));
  return host;
}

function gradeTable(rows, ctx) {
  const groups = new Map();
  for (const p of rows) {
    const k = `${p.experiment}|${p.objective || '?'}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  }
  if (!groups.size) return h('div.panel', empty('筛选之后一个样点都没有'));
  const code = (name) => S.summary.experiments.find((e) => e.name === name)?.code || '';
  const keys = [...groups.keys()].sort((a, b) => a.split('|')[1].localeCompare(b.split('|')[1]) || code(a.split('|')[0]).localeCompare(code(b.split('|')[0])));
  return h('div.panel.panel-body.flush.table-wrap', h('table.data',
    h('thead', h('tr', h('th', '实验'), h('th', '物镜'), ...GRADES.map((g) => h('th.num', g)), h('th.num', '无效'),
      h('th.num', '异常'), h('th.num', '中位分'), h('th.num', 'QFLS 中位数 (eV)'))),
    h('tbody', ...keys.map((k) => {
      const ps = groups.get(k);
      const [exp, obj] = k.split('|');
      const sc = ps.filter((p) => p.valid).map((p) => p.score).sort((a, b) => a - b);
      const md = ps.filter((p) => p.valid).map((p) => p.median).sort((a, b) => a - b);
      const mid = (v) => (v.length ? v[Math.floor((v.length - 1) / 2)] / 2 + v[Math.ceil((v.length - 1) / 2)] / 2 : null);
      return h('tr.is-clickable', clickableRow(() => { S.filter.exps = new Set([exp]); S.filter.objs = new Set([obj]); ctx.refresh(); }),
        h('td', h('span.mono.xsmall.dim', code(exp) + ' '), exp), h('td', obj),
        ...GRADES.map((g) => h('td.num', String(ps.filter((p) => p.grade === g).length || ''))),
        h('td.num', String(ps.filter((p) => !p.valid).length || '')),
        h('td.num', String(ps.filter((p) => p.anomaly).length || '')),
        h('td.num', sc.length ? mid(sc).toFixed(1) : '—'),
        h('td.num', md.length ? mid(md).toFixed(4) : '—'));
    }))));
}

function scanReport() {
  const sc = S.scan;
  return h('div',
    h('div.panel.panel-body.flush.table-wrap', h('table.data',
      h('thead', h('tr', h('th', '编号'), h('th', '实验'), h('th', '批次'), h('th.num', '点位'), h('th.num', '帧'))),
      h('tbody', ...sc.experiments.map((e) => h('tr',
        h('td.mono', e.code), h('td', e.name, e.name !== e.raw_name ? h('div.xsmall.dim', `磁盘上的名字是乱码「${e.raw_name}」，显示时修成了 UTF-8`) : null),
        h('td.small', ...e.batches.map((b) => h('div', h('span.mono.xsmall.dim', `${b.code} `), b.name,
          b.looks_like_hash ? null : h('span.xsmall.dim', '（不像哈希）')))),
        h('td.num', String(e.n_points)), h('td.num', String(e.n_frames))))))),
    h('div.row.gap-3.mt-3.small.muted.wrap',
      h('span', `跳过 ${sc.skipped.length} 处`), h('span', `历史分析产物 ${sc.artifacts} 个（只取 0 开头的 txt）`),
      h('span', `修过的乱码名 ${sc.renamed.length} 个`)),
    sc.skipped.length ? h('details.mt-2', h('summary.small', '跳过了哪些、为什么'),
      h('div.panel.panel-body.flush.table-wrap', h('table.data', h('tbody',
        ...sc.skipped.map((s) => h('tr', h('td.mono.xsmall', s.path), h('td.xsmall', s.kind === 'dir' ? '目录' : '文件'),
          h('td.xsmall.muted', s.reason))))))) : null,
    sc.artifact_examples?.length ? h('details.mt-2', h('summary.small', '历史产物举例'),
      h('div.mono.xsmall.muted', ...sc.artifact_examples.map((a) => h('div', a)))) : null);
}

// ================================================================== 排名与筛选
const RANK_COLS = ['score', 'median', 'p90_p10', 'low_2s_frac', 'low_cc_count', 'low_cc_max_frac', 'scratch_count',
                   'bright_density', 'morans_i', 'variogram_range_px', 'edge_minus_center', 'low_abs_frac'];

export function rankTab(ctx) {
  const defs = metricDefs();
  const rows = filtered();
  const k = S.sort.key;
  const sorted = rows.filter((p) => p.valid).sort((a, b) => {
    const va = S.valueMode === 'z' && k !== 'rank' ? a.z?.[k] : a[k];
    const vb = S.valueMode === 'z' && k !== 'rank' ? b.z?.[k] : b[k];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return S.sort.desc ? vb - va : va - vb;
  }).concat(rows.filter((p) => !p.valid));
  const setSort = (key) => {
    if (S.sort.key === key) S.sort.desc = !S.sort.desc;
    else S.sort = { key, desc: key === 'score' };
    ctx.refresh();
  };
  const th = (key, label) => h('th.num.qfls-sortable', {
    onclick: () => setSort(key), title: '点一下按它排序，再点一下反过来', tabindex: 0,
    onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSort(key); } },
    'aria-sort': S.sort.key === key ? (S.sort.desc ? 'descending' : 'ascending') : 'none',
  }, label, S.sort.key === key ? (S.sort.desc ? ' ↓' : ' ↑') : '');
  const cell = (p, key) => {
    if (!p.valid) return h('td.num.dim', '—');
    if (S.valueMode === 'z' && key !== 'score') {
      const z = p.z?.[key];
      return h('td.num' + (z != null && Math.abs(z) >= 2 ? '.qfls-z-hi' : ''), z == null ? '—' : z.toFixed(2));
    }
    return h('td.num', fmtMetric(key, p[key]));
  };
  const n = S.compare.length;
  return h('div',
    limits(S.valueMode === 'z' ? ' 组内 z = 同实验、同物镜里的 (x − 组均值) / 组标准差，|z| ≥ 2 标色。' : null),
    h('div.row.gap-3.mt-4.wrap',
      selectControl('排序', k, [['score', '综合分'], ['rank', '全局排名'], ...RANK_COLS.filter((c) => c !== 'score').map((c) => [c, defs[c]?.label || c])],
        (v) => { S.sort = { key: v, desc: v === 'score' }; ctx.refresh(); }),
      h('div.agent-seg', { role: 'radiogroup', 'aria-label': '显示' },
        ...[['raw', '原值'], ['z', '组内 z']].map(([v, t]) => h('button', {
          role: 'radio', 'aria-checked': String(S.valueMode === v), onclick: () => { S.valueMode = v; ctx.refresh(); },
        }, t))),
      h('span.small.muted', '单位：差值 meV、占比 %、密度 个/百万像素、变程 像素'),
      h('span.grow'),
      h('button.btn.btn-sm' + (n >= 2 ? '.btn-primary' : ''), {
        disabled: n < 2, onclick: () => ctx.nav('qfls', { arg: 'compare' }),
      }, `对比所选（${n} / ${COMPARE_MAX}）`)),
    sorted.length
      ? h('div.panel.panel-body.flush.table-wrap.mt-3', h('table.data.qfls-rank',
          h('thead', h('tr',
            h('th', h('span.sr-only', '加入对比')), th('rank', '排名'), h('th', '编号'), h('th', '实验 · 位置'), h('th', '物镜'),
            th('score', '分'), h('th', '等级'),
            ...RANK_COLS.filter((c) => c !== 'score').map((c) => th(c, defs[c]?.label || c)),
            h('th', '异常标记'))),
          h('tbody', ...sorted.map((p) => {
            const box = h('input', {
              type: 'checkbox', checked: S.compare.includes(p.key), disabled: !p.valid,
              'aria-label': `把 ${p.code} 加入对比`,
              onclick: (e) => {
                e.stopPropagation();
                if (!toggleCompare(p.key)) { e.preventDefault(); toast(`最多对比 ${COMPARE_MAX} 个`, 'warn'); }
                ctx.refresh();
              },
            });
            return h(`tr.is-clickable.lv-${p.level}`, { ...clickableRow(() => ctx.openPoint(p.key)), title: '打开这个样点' },
              h('td', box),
              h('td.num', p.rank ? String(p.rank) : '—', p.rank_in_objective ? h('div.xsmall.dim', `${p.objective} 内 ${p.rank_in_objective}`) : null),
              h('td.mono.small', p.code),
              h('td.small', p.experiment, h('div.xsmall.dim', `${p.batch} / ${p.slot} / ${p.point}`)),
              h('td', p.objective || '?'),
              p.valid ? h('td.num.strong', p.score.toFixed(1)) : h('td.num.dim', '—'),
              h('td', gradeChip(p.grade)),
              ...RANK_COLS.filter((c) => c !== 'score').map((c) => cell(p, c)),
              h('td', h('div.qfls-flags', ...p.flags.filter((f) => f.level !== 'info').map((f) =>
                h(`span.tag${f.level === 'error' ? '.tag-danger' : '.tag-warn'}`, { title: f.text }, FLAG_TEXT[f.code] || f.code)))));
          }))))
      : h('div.panel.mt-3', empty('筛选之后一个样点都没有')));
}

// ================================================================== 多点对比
export function compareTab(ctx) {
  const host = h('div');
  const keys = S.compare;
  const byKey = Object.fromEntries((S.points || []).map((p) => [p.key, p]));
  if (keys.length < 2) {
    const rows = filtered().filter((p) => p.valid);
    mount(host, limits(), h('div.panel.mt-4', empty(
      `在「排名与筛选」里勾 2 – ${COMPARE_MAX} 个样点来对比（现在选了 ${keys.length} 个）。`,
      h('div.row.gap-2',
        h('button.btn.btn-sm', { onclick: () => ctx.nav('qfls', { arg: 'rank' }) }, '去勾选'),
        rows.length >= 2 ? h('button.btn.btn-sm', {
          onclick: () => { setCompare([rows[0].key, rows[rows.length - 1].key]); ctx.redrawTabs(); },
        }, '当前筛选里分最高和最低的两个') : null))));
    return host;
  }
  const pts = keys.map((k) => byKey[k]).filter(Boolean);
  const objs = [...new Set(pts.map((p) => p.objective || '?'))];
  mount(host, skeletonRows(5));
  Promise.all(pts.map((p) => api.qflsPoint(S.runId, p.key))).then((details) => {
    if (!host.isConnected) return;
    mount(host, ...compareBody(ctx, pts, details, objs));
  }).catch((err) => mount(host, errorBox(err)));
  return host;
}

function colorbar(obj) {
  const [lo, hi] = S.summary.color_scales[obj] || [0, 1];
  const stops = S.summary.colormap.map((c, i, a) => `${c} ${(100 * i) / (a.length - 1)}%`).join(', ');
  return h('div.qfls-cbar', h('span.mono.xsmall', lo.toFixed(3)),
    h('i', { style: { background: `linear-gradient(90deg, ${stops})` } }), h('span.mono.xsmall', `${hi.toFixed(3)} eV`));
}

/** 对比用的一张叠加图：带「下载 ▾」（图片 / 数据）。 */
function overlay(title, key, spec, note = null) {
  const chart = xyChart(spec, { height: 260 });
  return figure(title, {
    head: downloadMenu({ svg: () => chart.querySelector('svg'), spec: () => spec, name: `QFLS_对比_${key}` }),
    body: chart,
    note: note ? h('div.chart-caption', note) : null,
  });
}

function compareBody(ctx, pts, details, objs) {
  const label = (p) => `${p.code}`;
  const series = (fn) => details.map((d, i) => ({ label: label(pts[i]), ...fn(d, pts[i]) }));
  const um = (p) => p.pixel_size_um;
  const hist = series((d) => {
    const e = d.extra.hist.edges, c = d.extra.hist.counts;
    const tot = c.reduce((a, b) => a + b, 0) || 1;
    return { x: c.map((_, i) => (e[i] + e[i + 1]) / 2), y: c.map((v) => v / tot) };
  });
  const allUm = pts.every((p) => um(p));
  const prof = series((d, p) => {
    const v = d.extra.profile_x;
    const len = p.cols * (allUm ? um(p) : 1);
    return { x: v.map((_, i) => (i * len) / Math.max(1, v.length - 1)), y: v };
  });
  const vario = series((d, p) => ({
    x: d.extra.variogram.lags_px.map((l) => l * (allUm ? um(p) : 1)),
    y: d.extra.variogram.gamma.map((g) => g * 1e6),
  }));
  const better = (key) => {
    const dir = BETTER[key];
    if (!dir) return null;
    const vals = pts.map((p) => p[key]).filter((v) => v != null);
    if (vals.length < 2) return null;
    return dir > 0 ? Math.max(...vals) : Math.min(...vals);
  };
  return [
    objs.length > 1
      ? limits(` 这几个样点物镜不同（${objs.join('、')}）：均值、中位数、低荧光绝对占比、连通域面积和数量、亮点密度、变程都不要横着比，看形状和组内 z。`)
      : limits(),
    h('div.row.gap-2.mt-4.wrap', ...pts.map((p) => h('span.qfls-pick',
      h('button.linklike.mono.small', { onclick: () => ctx.openPoint(p.key) }, p.code), gradeChip(p.grade),
      h('button.icon-btn', { 'aria-label': `从对比里去掉 ${p.code}`, title: '去掉',
        onclick: () => { toggleCompare(p.key); ctx.redrawTabs(); } }, '✕'))),
      h('button.btn.btn-xs.btn-ghost', { onclick: () => { setCompare([]); ctx.redrawTabs(); } }, '清空')),

    h('div.section.mt-4',
      h('div.section-head', h('div.section-title', '热图（同一物镜同一色标）'),
        h('div.section-note', '色标 = 该物镜所有帧 P1 的第 5 百分位 到 P99 的第 95 百分位；物镜之间色标不同、不可比')),
      h('div.qfls-grid', ...pts.map((p) => h('div.qfls-cell',
        h('div.small.strong.mono', p.code, h('span.muted', ` · ${p.experiment}`)),
        h('img.qfls-heat', { src: api.qflsFile(S.runId, `points/${p.key}/heatmap_group.png`), alt: `${p.code} 热图`, loading: 'lazy' }),
        colorbar(p.objective || '?'))))),

    h('div.section',
      h('div.qfls-grid',
        overlay('直方图（像素占比）', 'hist', { series: hist, x_label: 'QFLS (eV)', y_label: '像素占比' }),
        overlay('水平剖面（中间 5% 行的平均）', 'profile',
                { series: prof, x_label: allUm ? '位置 (µm)' : '位置（像素）', y_label: 'QFLS (eV)' }),
        overlay('变异函数', 'variogram', { series: vario, x_label: allUm ? '滞后 (µm)' : '滞后（像素）', y_label: 'γ (meV²)' },
                '横轴按各自的像素尺寸换成 µm；γ 第一次到 95% 基台的地方就是变程。'))),

    h('div.section',
      h('div.section-head', h('div.section-title', '指标对照'), h('div.section-note', '每行最好的那个加粗（只标有「好坏方向」的指标）')),
      h('div.panel.panel-body.flush.table-wrap', h('table.data',
        h('thead', h('tr', h('th', '指标'), h('th', '单位'), ...pts.map((p) => h('th.num.mono', p.code)))),
        h('tbody',
          h('tr', h('td', '物镜'), h('td'), ...pts.map((p) => h('td.num', p.objective || '?'))),
          h('tr', h('td', '等级'), h('td'), ...pts.map((p) => h('td.num', gradeChip(p.grade)))),
          ...S.summary.metrics.map((m) => {
            const best = objs.length > 1 && !m.cross_objective ? null : better(m.key);
            return h('tr', h('td', m.label, m.cross_objective ? null : h('span.xsmall.dim', ' （同物镜内比）')),
              h('td.xsmall.muted', unitOf(m.key) || (/_frac$/.test(m.key) ? '%' : '')),
              ...pts.map((p) => h('td.num' + (best !== null && p[m.key] === best ? '.strong.qfls-best' : ''),
                fmtMetric(m.key, p[m.key]),
                p.z?.[m.key] != null ? h('div.xsmall.dim', `z ${p.z[m.key].toFixed(2)}`) : null)));
          }),
          h('tr', h('td', '异常标记'), h('td'), ...pts.map((p) => h('td.small',
            p.flags.filter((f) => f.level !== 'info').map((f) => FLAG_TEXT[f.code] || f.code).join('、') || '—'))))))),
  ];
}
