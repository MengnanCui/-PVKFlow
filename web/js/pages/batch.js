// 批处理结果页（对比页）：进入页面、等批处理跑完、钉住的 AI 分析、运行状态表。
//
// 原来一个文件一千多行，2026-11 按职责拆开（tools/split_page.py），行为不变：
//   batch-state.js   页面状态 S / refs，和几处共用的小工具
//   batch-recipe.js  配方面板（改参数重跑）
//   batch-slices.js  时刻切片的两张膜厚图
//   batch-curves.js  曲线叠图 + 导出
// 依赖只有一个方向：本文件 → 其余四个 → batch-state.js。

import { api } from '../api.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, fmtInt, fmtTime, sampleLabel, confirmDialog,
} from '../ui.js';
import { drawChart, loadCurves } from './batch-curves.js';
import { drawRecipe, loadRecipe } from './batch-recipe.js';
import { loadSlices } from './batch-slices.js';
import { S, refs, resetRefs } from './batch-state.js';

export const meta = {
  id: 'batch',
  parent: 'history',
  title: '批处理结果',
  desc: '',
};

export function actions(nav) {
  return [
    h('button.btn.btn-sm', { onclick: () => nav('history') }, '← 对比历史'),
    h('button.btn.btn-sm', { onclick: () => nav('process') }, '挑样品'),
  ];
}

export async function view(host, ctx) {
  S.detail = S.error = null;
  S.curves = { integral: null, slope: null, ot: null };
  S.extra = [];
  S.slices = S.sliceErr = null;
  S.win2 = null; S.win2Resolved = false; S.tMax = null;
  S.modules = null; S.moduleErr = null; S.axes = null;
  S.pins = [];
  // alive()：这一次进入页面还算不算数。离开（或换成另一次对比）之后，
  // 还没回来的请求不许再改 S —— S 是模块级的，下一次进来的页面也用它。
  // 原来离开后轮询照跑，跑完把你此刻所在页面的标题改掉、把正在看的对比换掉（自查 §6 #6）。
  const alive = ctx.alive || (() => true);
  resetRefs({ host, nav: ctx.nav, alive });

  const arg = ctx.arg;
  if (!arg) {
    mount(host, empty('没有指定批处理', h('button.btn.btn-sm',
      { onclick: () => ctx.nav('process') }, '回到样品列表')));
    return;
  }

  // 参数可以是 task_… 也可以是 run_…。
  // 「选中几个样品 → 直接进对比页」那条路上，提交的一刻还没有 run_id ——
  // 让用户对着「正在跳转…」干等是没必要的，进来先看进度就是了。
  if (String(arg).startsWith('task_')) {
    S.taskId = arg;
    const done = await waitForTask(host, arg, ctx);
    if (!done) return;
    S.runId = done;
  } else {
    S.taskId = null;
    S.runId = arg;
  }

  mount(host, skeletonRows(8));
  let detail;
  try {
    detail = await api.batchDetail(S.runId);
  } catch (err) {
    if (alive()) mount(host, errorBox(err, () => view(host, ctx)));
    return;
  }
  if (!alive()) return;
  S.detail = detail;

  // 同事模块声明的批处理曲线：长表里有、对比页原来却画不出来（自查 §6 #9）。
  // 平台那三条有固定位置；其余能画的（不是 ot_ok 这种 0/1 辅助列）各占一格。
  S.extra = (S.detail.curves || []).filter((c) => c.plottable && !c.builtin);
  for (const c of S.extra) S.curves[c.column] = null;

  const params = S.detail.run.params || {};
  document.querySelector('#pageTitle').textContent = params.title || '对比结果';
  document.querySelector('#pageDesc').textContent =
    `${fmtInt(params.n_samples || S.detail.children.length)} 个样品 · `
    + fmtTime(S.detail.run.started_at);

  const recipeHost = h('div#recipeHost');
  refs.recipeHost = recipeHost;
  const chartHost = h('div#chartHost');
  const tableHost = h('div#tableHost');
  const pinHost = h('div#pinHost');
  const sliceHost = h('div#sliceHost');
  mount(host, header(), pinHost,
        h('div.section', sliceHost),
        h('div.section', chartHost), h('div.section', tableHost));
  refs.chartHost = chartHost;
  refs.tableHost = tableHost;
  refs.pinHost = pinHost;
  refs.sliceHost = sliceHost;

  drawRecipe();
  drawChart();
  drawTable();
  loadRecipe();
  loadCurves();
  loadSlices();
  loadPins();
}

/**
 * 等这次批处理跑完，边等边显示进度。
 *
 * @returns parent_run_id；失败或取消时返回 null（错误已经画在页面上了）
 */
async function waitForTask(host, taskId, ctx) {
  const bar = h('i');
  const msg = h('div.small.muted.mt-2', '正在排队…');
  mount(host, h('div.section',
    h('div.panel.panel-body',
      h('div.strong', '正在对比'),
      h('div.progress-bar.mt-3', bar),
      msg,
      h('div.mt-3', h('button.btn.btn-sm', {
        onclick: async () => { await api.cancelTask(taskId).catch(() => {});
                               ctx.nav('process'); },
      }, '取消')))));

  const alive = ctx.alive || (() => true);
  for (;;) {
    if (!alive()) return null;        // 你已经走了：不再轮询，也不再往页面上写
    let t;
    try {
      t = await api.getTask(taskId);
    } catch (err) {
      if (alive()) mount(host, errorBox(err, () => view(host, ctx)));
      return null;
    }
    if (!alive()) return null;
    const pct = t.total ? Math.round((t.progress / t.total) * 100) : 0;
    bar.style.width = `${pct}%`;
    msg.textContent = t.message || `${t.progress} / ${t.total}`;

    if (t.status === 'ok') return t.result?.parent_run_id || null;
    if (t.status === 'failed' || t.status === 'cancelled') {
      // 取消是你自己按的，不是「没跑成」—— 把它说成失败会让人以为出了问题。
      mount(host, errorBox(
        { message: t.error
            || (t.status === 'cancelled' ? '这次对比被取消了' : '这次对比没跑成'),
          detail: t.message },
        () => ctx.nav('process'), '回到样品列表'));
      return null;
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}

// ------------------------------------------------------------------ 钉住的分析
//
// 「比完就没了、找不到在哪儿」的最后一环。对比本身靠 analysis_run 留住了，
// AI 对那次对比说过的话靠这里 —— 钉住的是正文快照，删掉对话也带不走它。
async function loadPins() {
  const alive = refs.alive;
  let pins;
  try {
    pins = (await api.pins(S.runId)).pins || [];
  } catch {
    pins = [];          // 钉住是锦上添花，取不到不该影响看结果
  }
  if (!alive()) return;
  S.pins = pins;
  drawPins();
}

function drawPins() {
  if (!refs.pinHost) return;
  if (!S.pins.length) { clear(refs.pinHost); return; }
  mount(refs.pinHost, h('div.section',
    h('div.panel',
      h('div.panel-head',
        h('div.panel-title', `AI 分析 · ${fmtInt(S.pins.length)} 条`),
        h('span.xsmall.dim', '从 AI 助手里钉过来的')),
      h('div.panel-body',
        ...S.pins.map((p) => h('div.mb-3',
          h('div.pin-head',
            h('span.xsmall.dim',
              `${p.conversation_title || '已删除的对话'} · ${fmtTime(p.created_at)}`),
            h('button.ai-act', {
              onclick: async () => {
                // 对话删掉之后，钉住的这段就是唯一的一份 —— 先问
                if (!await confirmDialog('取消钉住这段分析？对话已经删掉的话，这段文字就找不回来了。',
                  { confirmLabel: '取消钉住', danger: true })) return;
                try {
                  await api.deletePin(p.pin_id);
                } catch (err) { toast(err.message, 'err'); return; }
                if (!refs.alive()) return;
                S.pins = S.pins.filter((x) => x.pin_id !== p.pin_id);
                drawPins();
                toast('已取消钉住');
              },
            }, '取消钉住')),
          h('div.pin-note', p.note)))))));
}

function header() {
  const d = S.detail;
  return h('div.section',
    h('div.metrics.metrics-4',
      metric('样品', d.children.length, '本次处理'),
      metric('成功', d.n_ok, d.n_ok === d.children.length ? '全部通过' : ''),
      metric('失败', d.n_failed, d.n_failed ? '见下表' : '无'),
      metric('曲线表', d.tables.length ? d.tables[0].n_rows : 0, '长表行数')),
    refs.recipeHost);
}

const metric = (label, value, note) => h('div.metric',
  h('div.metric-label', label),
  h('div.metric-value' + (value ? '' : '.is-zero'), fmtInt(value)),
  note ? h('div.metric-note', note) : null);

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
