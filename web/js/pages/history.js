// 对比历史 —— 每一次批处理就是一次对比，这里是它们的入口。
//
// 这份数据和接口早就在了（GET /api/batch/runs、app/batch.py 的 recent_batches），
// 缺的只是一个能点进去的地方。比完就没了、找不到在哪儿，就是因为
// 唯一的入口是跑完那一下的「看结果」按钮，页面一刷新就没了。

import { api } from '../api.js';
import { withInfo } from '../components/info.js';
import { h, render, empty, fmtInt, fmtTime, statusText, rowLink, modal, busy, toast, confirmDialog } from '../ui.js';

export const meta = {
  id: 'history',
  title: '对比历史',
  desc: '每一次批处理都留在这里，随时点回去',
};

export function actions(nav) {
  return [h('button.btn.btn-sm', { onclick: () => nav('process') }, '去挑样品')];
}

export async function view(host, ctx) {
  // 两个请求一起发。钉住计数一次查完，不是一行一个查询。
  await render(host, async () => {
    const [d, pins] = await Promise.all([
      api.with(ctx.signal).batchRuns(),
      api.with(ctx.signal).pinCounts().catch(() => ({ counts: {} })),   // 取不到就当没有，不挡住列表
    ]);
    return { ...d, pinCounts: pins.counts || {} };
  }, (d) => {
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
            h('th', withInfo('AI', 'ai_scope')), h('th', '配方'), h('th', '时间'), h('th', ''))),
          h('tbody', ...runs.map((r) => row(r, host, ctx, d.pinCounts[r.analysis_run_id] || 0))))));
  });
}

function row(r, host, ctx, nPins) {
  const p = parseParams(r.params_json);
  const recipe = p.recipe || {};
  // 波长不加千分位
  const band = recipe.band_min
    ? `${Number(recipe.band_min)}–${Number(recipe.band_max)} nm`
    : '—';
  const name = p.title || '未命名对比';
  const reload = () => view(host, ctx);

  // 一行点进那次的结果；键盘也能打开（Tab 到行上，回车或空格）。行里的「改名」「删除」不算点行
  return rowLink(() => ctx.nav('batch', { arg: r.analysis_run_id }), { dataset: { run: r.analysis_run_id } },
    h('td.strong', h('span.run-name', name), runStatus(r.status)),
    h('td', fmtInt(r.n_children)),
    h('td', r.n_failed
      ? h('span.status.status-warn', `${fmtInt(r.n_failed)} 个`)
      : h('span.dim', '无')),
    h('td', nPins ? h('span.tag.tag-accent', `📌 ${fmtInt(nPins)}`) : h('span.dim', '—')),
    h('td.small.muted', band),
    h('td.small.muted', fmtTime(r.started_at)),
    h('td.nowrap.row-actions',
      h('button.btn.btn-sm.btn-ghost', { onclick: () => renameDialog(r.analysis_run_id, name, reload) }, '改名'),
      h('button.btn.btn-sm.btn-ghost', {
        disabled: r.status === 'running', title: r.status === 'running' ? '还在跑，停下来才能删' : '',
        onclick: (e) => deleteRun(e.currentTarget, r.analysis_run_id, name, reload),
      }, '删除')));
}

// ------------------------------------------------------------------ 改名 / 删除（C077）
function renameDialog(runId, current, done) {
  const input = h('input.input', { type: 'text', value: current, maxlength: 120 });
  const save = async (e) => {
    busy(e.target, true);
    try {
      await api.renameBatchRun(runId, input.value);
      m.close();
      toast('已改名', 'ok');
      done();
    } catch (err) { busy(e.target, false); toast(err.message, 'err'); }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save({ target: m.panel.querySelector('.btn-primary') }); });
  const m = modal({
    title: '给这次对比改个名', width: '480px',
    body: h('div', h('p.small.muted', '只改对比历史里显示的名字，结果不动。'), h('div.mt-3', input)),
    foot: [h('button.btn', { onclick: () => m.close() }, '取消'), h('button.btn.btn-primary', { onclick: save }, '保存')],
  });
  input.select();
}

/** 删之前先说清楚会带走什么（app/batch_history.py footprint），不让人盲删。 */
async function deleteRun(btn, runId, name, done) {
  busy(btn, true);
  let fp;
  try { fp = await api.batchRunFootprint(runId); } catch (err) { toast(err.message, 'err'); busy(btn, false); return; }
  busy(btn, false);
  const lines = [
    `「${name}」：${fmtInt(fp.n_samples)} 个样品、${fmtInt(fp.n_results)} 条关键结果`
      + (fp.n_tables ? `、${fmtInt(fp.n_tables)} 张曲线表` : '')
      + (fp.n_pins ? `、${fmtInt(fp.n_pins)} 条钉住的 AI 分析` : '') + '会一起删掉。',
    fp.n_current
      ? `其中 ${fmtInt(fp.n_current)} 条现在是样品的「当前值」—— 删了以后退回上一次对比算的；没有上一次的样品就没有值了。`
      : '这些结果都不是样品现在的「当前值」，删了不影响别处的数。',
    fp.n_reviewed ? `${fmtInt(fp.n_reviewed)} 条做过审核（可信 / 待查 / 作废和理由），也一起没了。` : null,
    '原始数据、样品、别的对比、AI 对话本身都不动。删了不能撤销。',
  ].filter(Boolean);
  const ok = await confirmDialog(h('span', ...lines.map((t) => h('span.block.mb-2', t))),
                                 { confirmLabel: '删除', danger: true });
  if (!ok) return;
  try {
    await api.deleteBatchRun(runId);
    toast(`已删掉「${name}」`, 'ok');
    done();
  } catch (err) { toast(err.message, 'err', 6000); }
}

// 没跑完的要看得出来：取消了只算了一部分、服务重启时被打断、还在跑。
// 原来这一列不显示状态，一次只算了 3/40 就被取消的对比，看起来和跑完的一模一样。
function runStatus(status) {
  const label = { cancelled: `${statusText('cancelled')} · 只有一部分`, failed: `${statusText('failed')} · 没跑完`,
                  running: statusText('running') }[status];
  return label ? h('span.status.status-warn.small', { style: { marginLeft: '8px' } }, label) : null;
}

function parseParams(raw) {
  try { return JSON.parse(raw || '{}'); } catch { return {}; }
}
