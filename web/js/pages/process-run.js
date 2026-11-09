// 数据处理页：把选中的样品交给批处理 —— 确认对话框、排队、进度、跑完跳到对比页。

import { api } from '../api.js';
import { h, toast, busy, fmtInt, modal, statusText } from '../ui.js';
import { S, hooks, refs } from './process-state.js';

// 「选择即示例」那个建议条删掉了：它挂在列表上方向下弹出，一出现就把
// 下面的样品行推走，正在挑样品的时候根本点不中。后端 /api/selection/suggest
// 和 selection.suggest_expansion 都留着 —— 以后想要，换个不挡路的位置接回来。

// ------------------------------------------------------------------ 批处理
/** 选中几个 → 直接开跑 → 直接落到对比页（页面上自己显示进度）。 */
export async function startCompare(btn) {
  const useChecked = S.checked.size > 0;
  const filter = useChecked ? { ids: [...S.checked] } : S.filter;

  busy(btn, true);
  try {
    const pv = await api.batchPreview(filter);
    if (!pv.with_matrix) {
      toast('选中的样品里没有光谱矩阵，没法对比', 'err');
      return;
    }
    if (pv.without_matrix) {
      // 跳过了一些样品要说出来，别让人以为全都比了
      toast(`${pv.without_matrix} 个样品没有光谱矩阵，这次跳过`, 'warn');
    }
    const stamp = new Date().toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    const r = await api.batchRun({
      // 配方留空 = 用后端的默认配方。默认值只在模块的控件声明里有一份
      // （膜厚 775–1120、积分 800–950、斜率 950 ± 10），这里不再抄。
      filter, recipe: {},
      title: `${pv.with_matrix} 个样品 · ${stamp}`,
    });
    // 传 task_id：这一刻还没有 parent_run_id，对比页会自己等
    refs.nav('batch', { arg: r.task.task_id });
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    busy(btn, false);
  }
}

export async function batchDialog(recipeOverride = null) {
  const useChecked = S.checked.size > 0;
  const filter = useChecked ? { ids: [...S.checked] } : S.filter;

  let pv;
  try {
    pv = await api.batchPreview(filter);
  } catch (err) { toast(err.message, 'err'); return; }

  if (!pv.with_matrix) {
    toast('选中的样品里没有光谱矩阵', 'err');
    return;
  }

  // 默认值由后端按模块声明给（pv.recipe_defaults），这里不再抄一份。
  // AI 卡片带来的配方只是**预填**，对话框照样要你确认才开跑。
  const recipe = { ...(pv.recipe_defaults || {}), ...(recipeOverride || {}) };
  const stamp = new Date().toLocaleString('zh-CN', { month: '2-digit', day: '2-digit',
                                                    hour: '2-digit', minute: '2-digit' });
  let title = `${pv.with_matrix} 个样品 · ${stamp}`;
  const nameInput = h('input.input', {
    type: 'text', value: title, placeholder: '这次对比叫什么',
    oninput: (e) => { title = e.target.value; },
  });
  const num = (key, label, unit) => h('div.field',
    h('label.field-label', label, unit ? h('span.unit', ` (${unit})`) : null),
    h('input.input', { type: 'number', value: recipe[key], step: 'any',
      oninput: (e) => { recipe[key] = Number(e.target.value); } }));

  const m = modal({
    title: `批处理 ${fmtInt(pv.with_matrix)} 个样品`,
    width: '620px',
    body: h('div',
      h('p.small.muted',
        `命中 ${fmtInt(pv.total)} 个样品，其中 ${fmtInt(pv.with_matrix)} 个有光谱矩阵`,
        pv.without_matrix ? `，${fmtInt(pv.without_matrix)} 个会被跳过` : '', '。'),
      h('div.field.mt-3',
        h('label.field-label', '名称',
          h('span.unit', ' （会出现在对比历史里，方便以后找回来）')),
        nameInput),
      h('div.notice.mt-3',
        h('div.grow',
          h('div.small', '这套参数就是单样品页上的那套 —— '
            + '在单个样品上调好之后回到这里，填一样的值即可。')),
      ),
      h('div.recipe-grid.mt-4',
        num('band_min', '膜厚窗口起', 'nm'), num('band_max', '膜厚窗口止', 'nm'),
        num('integral_min', '积分波段起', 'nm'), num('integral_max', '积分波段止', 'nm'),
        num('slope_center', '斜率波长', 'nm'), num('slope_half_width', '斜率窗口半宽', 'nm'))),
    foot: [
      h('span.small.muted.grow', '跑在后台，可以离开这个页面'),
      h('button.btn', { onclick: () => m.close() }, '取消'),
      h('button.btn.btn-primary', {
        onclick: async (e) => {
          busy(e.target, true);
          try {
            const r = await api.batchRun({ filter, recipe,
              title: title.trim() || `${pv.with_matrix} 个样品` });
            m.close();
            S.task = r.task;
            hooks.drawList();
            startPolling(r.task.task_id);
          } catch (err) {
            busy(e.target, false);
            toast(err.message, 'err', 6000);
          }
        },
      }, '开始'),
    ],
  });
}

export function progressBlock() {
  const t = S.task;
  const pct = t.percent ?? 0;
  return h('div.progress-card.mb-3',
    h('div.row-between',
      h('div',
        h('div.small.strong', t.title || '批处理'),
        h('div.xsmall.dim.mt-2', t.message || statusText(t.status))),
      h('div.row.gap-2',
        t.done
          ? h('button.btn.btn-sm', {
              onclick: () => { S.task = null; hooks.drawList(); } }, '收起')
          : h('button.btn.btn-sm', {
              onclick: async (e) => {
                busy(e.target, true);
                try { await api.cancelTask(t.task_id); } catch (err) { toast(err.message, 'err'); }
                busy(e.target, false);
              } }, '取消'),
        t.done && t.status === 'ok' && t.result?.parent_run_id
          ? h('button.btn.btn-primary.btn-sm', {
              onclick: () => refs.nav('batch', { arg: t.result.parent_run_id }),
            }, '看结果')
          : null)),
    h('div.progress-bar', h('i', { style: { width: `${pct}%` } })),
    h('div.row-between',
      h('span.xsmall.dim', `${fmtInt(t.progress)} / ${fmtInt(t.total)}`),
      h('span.xsmall.dim',
        t.n_failed ? `${fmtInt(t.n_ok)} 成功 · ${fmtInt(t.n_failed)} 失败`
                   : `${fmtInt(t.n_ok)} 成功`)));
}


function startPolling(taskId) {
  stopPolling();
  // 只在你还在这一页时轮询。原来离开之后照跑，跑完了把你从别的页面
  // **强行拉到对比页** —— 你可能正看着别的东西。离开了就停；结果在「对比历史」里。
  const alive = refs.alive || (() => true);
  let misses = 0;
  S.pollTimer = setInterval(async () => {
    if (!alive()) { stopPolling(); return; }
    try {
      const t = await api.getTask(taskId);
      if (!alive()) { stopPolling(); return; }
      misses = 0;
      S.task = t;
      hooks.drawList();
      if (t.done) {
        stopPolling();
        if (t.status === 'ok') {
          toast(`处理完成：${t.n_ok} 成功` + (t.n_failed ? `，${t.n_failed} 失败` : ''), 'ok');
          // 跑完直接落到对比页。以前还要再点一下「看结果」，而那个按钮
          // 一刷新就没了 —— 「比完就没了、找不到在哪儿」说的就是这个。
          // 现在它同时也进了「对比历史」，随时点得回来。
          if (t.result?.parent_run_id) {
            refs.nav('batch', { arg: t.result.parent_run_id });
            return;
          }
          hooks.reload();
        } else if (t.status === 'failed') {
          toast(t.error?.split('\n')[0] || '批处理失败', 'err', 8000);
        }
      }
    } catch (err) {
      // 原来一次取不到就悄悄停掉，进度条从此卡住。偶尔一次失败再等等；
      // 连着三次就停下并说出来
      if (++misses >= 3) {
        stopPolling();
        if (alive()) toast(`取不到批处理进度：${err.message}。结果会出现在「对比历史」里`, 'warn', 8000);
      }
    }
  }, 700);
}

export function stopPolling() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
}
