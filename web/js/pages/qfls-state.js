// QFLS 成像几页共用的状态：当前看的是哪次处理、筛选条件、对比篮子。
//
// 筛选在「总览 / 排名 / 对比」三个标签和单点详情之间共用 —— 在排名里筛了 100X，
// 点进一个样点再「下一个」，走的是筛出来的那串。

import { api } from '../api.js';
import { saveBlob } from '../download.js';

export const S = {
  runId: null, run: null, summary: null, scan: null, points: null,
  filter: { exps: new Set(), objs: new Set(), grades: new Set(), flag: null, anomalyOnly: false, q: '' },
  compare: [],
  sort: { key: 'score', desc: true },
  valueMode: 'raw',            // raw | z
  metric: 'median',            // 总览箱线图看哪个指标
};

const RUN_KEY = 'hte.qfls.run';
const CMP_KEY = 'hte.qfls.compare';
export const COMPARE_MAX = 6;

const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 隐私模式 */ } };

export const rememberedRun = () => load(RUN_KEY, null);

/** 换到某次处理：取它的汇总和样点表。筛选清空，对比篮子按这次处理记。 */
export async function openRun(runId) {
  const [d, p] = await Promise.all([api.qflsRun(runId), api.qflsPoints(runId).catch(() => ({ points: null }))]);
  S.runId = runId;
  S.run = d.run; S.summary = d.summary; S.scan = d.scan; S.points = p.points;
  const f = S.filter;
  f.exps.clear(); f.objs.clear(); f.grades.clear(); f.flag = null; f.anomalyOnly = false; f.q = '';
  const keys = new Set((S.points || []).map((x) => x.key));
  S.compare = (load(CMP_KEY, {})[runId] || []).filter((k) => keys.has(k));
  save(RUN_KEY, runId);
  return S;
}

export function clearRun() {
  S.runId = null; S.run = null; S.summary = null; S.scan = null; S.points = null; S.compare = [];
  save(RUN_KEY, null);
}

export function setCompare(keys) {
  S.compare = keys.slice(0, COMPARE_MAX);
  const all = load(CMP_KEY, {});
  all[S.runId] = S.compare;
  save(CMP_KEY, all);
}

export function toggleCompare(key) {
  if (S.compare.includes(key)) setCompare(S.compare.filter((k) => k !== key));
  else if (S.compare.length < COMPARE_MAX) setCompare([...S.compare, key]);
  else return false;
  return true;
}

/** 当前筛选下的样点（保持排名顺序：有效的按分数，无效的在最后）。 */
export function filtered() {
  const f = S.filter;
  const q = f.q.trim().toLowerCase();
  return (S.points || []).filter((p) =>
    (!f.exps.size || f.exps.has(p.experiment))
    && (!f.objs.size || f.objs.has(p.objective || '?'))
    && (!f.grades.size || f.grades.has(p.grade || '无效'))
    && (!f.flag || p.flag_codes.includes(f.flag))
    && (!f.anomalyOnly || p.anomaly)
    && (!q || `${p.code} ${p.experiment} ${p.batch} ${p.slot} ${p.point} ${p.file}`.toLowerCase().includes(q)));
}

export const filterActive = () => {
  const f = S.filter;
  return f.exps.size || f.objs.size || f.grades.size || f.flag || f.anomalyOnly || f.q.trim();
};

/** 指标定义（键 → {label, unit, cross_objective}），来自 summary.json。 */
export const metricDefs = () => Object.fromEntries((S.summary?.metrics || []).map((m) => [m.key, m]));

export const FLAG_TEXT = {
  invalid: '数据无效', out_of_range: '超范围像素', name_problem: '文件名不规范', meta_mismatch: '物镜与 qvdf 不符',
  no_qvdf: '缺 qvdf', qvdf_name: 'qvdf 名字不对', large_low_area: '大面积低值', scratch: '划痕',
  structure: '结构不均', edge_effect: '边缘效应', group_outlier: '平行点离群', flat: '数值不变',
  shape_differs: '尺寸不同', condition_differs: '条件不同', variogram_open: '变程未到基台',
};

// 越小越好 / 越大越好（对比表里标最好的那个；mean / median 这类跨物镜不可比的不标）
export const BETTER = {
  score: 1, std: -1, p90_p10: -1, p99_p1: -1, low_2s_frac: -1, high_2s_frac: -1, low_cc_count: -1,
  low_cc_max_frac: -1, scratch_count: -1, bright_density: -1, morans_i: -1,
};

export function fmtMetric(key, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (/_frac$/.test(key)) return `${(v * 100).toFixed(v < 0.001 && v > 0 ? 3 : 2)}%`;
  if (['mean', 'median', 'p1', 'p10', 'p90', 'p99', 'edge_mean', 'center_mean'].includes(key)) return v.toFixed(4);
  if (['std', 'p90_p10', 'p99_p1', 'edge_minus_center'].includes(key)) return `${(v * 1000).toFixed(1)}`;
  if (['low_cc_count', 'scratch_count', 'bright_count', 'rows', 'cols'].includes(key)) return String(Math.round(v));
  if (key === 'morans_i') return v.toFixed(3);
  if (key === 'score') return v.toFixed(1);
  if (Math.abs(v) >= 100) return v.toFixed(0);
  return Number(v.toPrecision(3)).toString();
}

/** 显示用的单位：eV 差值改成 meV 显示（fmtMetric 已乘 1000）。 */
export function unitOf(key) {
  if (['std', 'p90_p10', 'p99_p1', 'edge_minus_center'].includes(key)) return 'meV';
  const d = metricDefs()[key];
  return d?.unit && !/_frac$/.test(key) ? d.unit : '';
}

/** 箱线图统计（前端按当前筛选现算）。 */
export function boxStats(name, items) {
  const v = items.map((x) => x.y).sort((a, b) => a - b);
  if (!v.length) return { name, n: 0 };
  const q = (p) => { const i = (v.length - 1) * p; const lo = Math.floor(i); return v[lo] + (v[Math.ceil(i)] - v[lo]) * (i - lo); };
  const q1 = q(0.25), med = q(0.5), q3 = q(0.75), iqr = q3 - q1;
  const inside = v.filter((x) => x >= q1 - 1.5 * iqr && x <= q3 + 1.5 * iqr);
  return {
    name, n: v.length, q1, median: med, q3,
    whisker_lo: Math.min(...inside), whisker_hi: Math.max(...inside),
    outliers: items.filter((x) => x.y < q1 - 1.5 * iqr || x.y > q3 + 1.5 * iqr),
  };
}

export function download(name, text, type = 'text/csv;charset=utf-8') {
  saveBlob(new Blob([text], { type }), name);
}
