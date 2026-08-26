// 对比历史 —— 每一次批处理就是一次对比，这里是它们的入口。
//
// 这份数据和接口早就在了（GET /api/batch/runs、app/batch.py 的 recent_batches），
// 缺的只是一个能点进去的地方。比完就没了、找不到在哪儿，就是因为
// 唯一的入口是跑完那一下的「看结果」按钮，页面一刷新就没了。

import { api } from '../api.js';
import { h, mount, render, empty, fmtInt, fmtTime } from '../ui.js';

export const meta = {
  id: 'history',
  title: '对比历史',
  desc: '每一次批处理都留在这里，随时点回去',
};

export function actions(nav) {
  return [h('button.btn.btn-sm', { onclick: () => nav('process') }, '去挑样品')];
}

export async function view(host, ctx) {
  await render(host, () => api.batchRuns(), (d) => {
    const runs = d.runs || [];
    if (!runs.length) {
      return empty('还没有跑过对比',
        h('button.btn.btn-sm', { onclick: () => ctx.nav('process') }, '去挑样品'));
    }
    return h('div.panel',
      h('div.panel-head',
        h('div.panel-title', `${fmtInt(runs.length)} 次对比`),
        h('span.xsmall.dim', '点一行回到那次的结果')),
      h('div.panel-body.flush',
        h('table.data',
          h('thead', h('tr',
            h('th', '名称'), h('th', '样品'), h('th', '失败'),
            h('th', '配方'), h('th', '时间'))),
          h('tbody', ...runs.map((r) => row(r, ctx))))));
  });
}

function row(r, ctx) {
  const p = parseParams(r.params_json);
  const recipe = p.recipe || {};
  // 波长不加千分位
  const band = recipe.band_min
    ? `${Number(recipe.band_min)}–${Number(recipe.band_max)} nm`
    : '—';

  return h('tr.is-clickable', {
    onclick: () => ctx.nav('batch', { arg: r.analysis_run_id }),
  },
    h('td.strong', p.title || '未命名对比'),
    h('td', fmtInt(r.n_children)),
    h('td', r.n_failed
      ? h('span.status.status-warn', `${fmtInt(r.n_failed)} 个`)
      : h('span.dim', '无')),
    h('td.small.muted', band),
    h('td.small.muted', fmtTime(r.started_at)));
}

function parseParams(raw) {
  try { return JSON.parse(raw || '{}'); } catch { return {}; }
}
