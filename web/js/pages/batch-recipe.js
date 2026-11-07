// 批处理结果页：配方面板 —— 这次对比用的参数，改了可以重跑。
// 参数面板照模块声明生成（模块自己说有哪些参数、范围是多少）。

import { api } from '../api.js';
import { withInfo } from '../components/info.js';
import { h, mount, toast, busy, fmtInt, fmtNum } from '../ui.js';
import { specControl } from '../components/figure.js';
import { S, refs } from './batch-state.js';

// ------------------------------------------------------------------ 参数面板
//
// 面板上有哪些参数，**由已装模块的声明决定**，这个文件里一个控件都不写死。
//
// 上一版是手写的六个框（膜厚窗口起止、积分起止、斜率波长、斜率半宽）——
// 那是模块化之前的清单。模块化做完两轮之后，同事装一个模块进来，
// 它声明的控件在样品页上会自动出现，在这一页上却够不着：
// 后端 `Recipe.module_params` 明明认这个结构，前端一个控件都不生成。
// 于是「可调参数」实际上就是那六个，别的全是模块里的默认值。
export async function loadRecipe() {
  const alive = refs.alive;
  try {
    const [mods, axes] = await Promise.all([
      api.modules(),
      // 波长/时间轴范围。取不到就让控件退回自己声明的 min/max ——
      // 滑块照样能用，只是范围不跟着这批数据。
      api.batchAxes(S.runId).catch(() => ({ available: false })),
    ]);
    if (!alive()) return;
    S.modules = mods.modules || [];
    S.axes = axes.available ? axes : null;
  } catch (err) {
    if (!alive()) return;
    S.moduleErr = err;
  }
  drawRecipe();
}

export function drawRecipe() {
  if (!refs.recipeHost) return;
  mount(refs.recipeHost, recipePanel());
}

/**
 * 配方面板。**进来先用默认值比出结果，参数摆在这儿随时改。**
 *
 * 原来是进对比之前先弹一个对话框填六个数 —— 可你在填的时候还没看到任何东西，
 * 六个数该填什么根本无从判断。现在反过来：先看到结果，觉得窗口不对再改。
 *
 * 改完是**新的一次对比**，旧的那次留在对比历史里。参数一改结果就变，
 * 原地覆盖的话你就没法说「上次那个窗口下是什么样」了。
 */
function recipePanel() {
  // 控件里填什么值由后端算好（`effective_params`：默认值 ← 老配方 ← 显式指定）。
  // 那条规则只有 app/batch.py 里一份实现，前端再抄一遍迟早对不上。
  const recipe = { ...(S.detail.run.params || {}).recipe };
  recipe.module_params = JSON.parse(JSON.stringify(S.detail.module_params || {}));

  const groups = (S.modules || [])
    // 只列**参与批处理**的模块。一个模块不产出批处理曲线，它的参数在这一页上
    // 调了也不会改变任何东西 —— 摆一个不起作用的旋钮比不摆更糟。
    .filter((m) => (m.batch_curves || []).length && (m.controls || []).length)
    .sort((a, b) => (a.order ?? 100) - (b.order ?? 100));

  const body = S.moduleErr
    ? h('div.notice.notice-warn', h('div.grow',
        h('div.small', `读不到已装模块：${S.moduleErr.message}`),
        h('p.xsmall.dim.mt-2', '参数面板画不出来，但下面的结果是好的。')))
    : !S.modules
      ? h('div.xsmall.dim', '正在读已装模块…')
      : h('div.recipe-grid',
          ...groups.map((m) => h('div.recipe-group',
            h('div.xsmall.dim.mb-2', m.name),
            h('div.row.gap-4.wrap.items-end',
              ...(m.controls || []).map((c) => moduleControl(m, c, recipe))))),
          h('div.recipe-group',
            h('div.xsmall.dim.mb-2', '平台'),
            h('div.row.gap-4.wrap.items-end', maxTimeControl(recipe))));

  const rerun = h('button.btn.btn-sm.btn-primary', {
    onclick: async (e) => {
      busy(e.target, true);
      try {
        const r = await api.batchRun({
          filter: (S.detail.run.params || {}).filter || {},
          recipe,
          title: `${S.detail.n_ok} 个样品 · ${rerunTag(recipe)}`,
        });
        // 跳到新的一次 —— 旧的那次原样留在对比历史里
        refs.nav('batch', { arg: r.task.task_id });
      } catch (err) {
        toast(err.message, 'danger');
        busy(e.target, false);
      }
    },
  }, '按新参数重跑');

  return h('div.figure-ctl.mt-4', { style: { display: 'block' } },
    h('div.row.gap-3.items-center.mb-3',
      h('span.small.strong', '参数'),
      h('span.xsmall.dim', groups.length
        ? `来自 ${fmtInt(groups.length)} 个模块的声明`
        : ''),
      h('span.grow'),
      rerun),
    body,
    h('div.xsmall.dim.mt-3',
      '这些参数改完要重跑一次 —— 它们决定曲线本身怎么算。',
      // 原来这里是纯文本里写了 **不用**，星号原样显示；而且那一块在参数区**下面**，
      // 文案却写「上面」（自查 §6 #27）
      '下面「不同时刻的平均膜厚」里换时刻', h('strong', '不用'),
      '重跑：膜厚曲线整条已经存下来了。'));
}

/** 重跑标题里带上最能说明这次改了什么的那个参数。 */
function rerunTag(recipe) {
  const band = recipe.module_params?.['thickness.fringe_ot']?.band;
  return Array.isArray(band) ? `${band[0]}–${band[1]} nm`
                             : `${recipe.band_min}–${recipe.band_max} nm`;
}

/**
 * 按一条控件声明渲染一个控件，值写进 `recipe.module_params[模块][控件]`。
 *
 * 控件怎么从声明长出来，和样品页的模块面板共用一份（figure.specControl）。
 * 这里只改配方、不打后端：改完点「重跑」才算，所以不给 onLive / onCommit。
 */
function moduleControl(m, c, recipe) {
  const bag = (recipe.module_params[m.id] ||= {});
  if (bag[c.key] === undefined) bag[c.key] = c.default;
  const axis = (kind) => (S.axes && kind === 'lambda' ? S.axes.lambda
                        : S.axes && kind === 'time' ? S.axes.time : null);
  return specControl(c, { get: () => bag[c.key], set: (v) => { bag[c.key] = v; }, axis });
}

/**
 * 时间抽稀。这一个是**平台**的参数，不属于任何模块 ——
 * 它决定长表存多少行，对每个模块都一样。上一版整个漏了。
 */
function maxTimeControl(recipe) {
  return h('label.inline-field',
    h('span.small.muted', withInfo('时间点上限', 'max_time_points', {
      spot: () => ({
        title: '这次对比的时间抽稀设置',
        rows: {
          当前值: recipe.max_time_points ? `${recipe.max_time_points} 个时间点`
                                         : '0（全部帧，不抽稀）',
          长表行数: fmtInt(S.detail?.tables?.[0]?.n_rows || 0),
          样品数: fmtInt(S.detail?.children?.length || 0),
          这批数据的时间范围: S.tMax ? `0–${fmtNum(S.tMax, 3)} s` : '（还没读到）',
        },
        note: '膜厚不受这个设置影响 —— 膜厚模块声明了要拿全部帧，存在另一张表里。',
        scope: { run_id: S.runId },
      }),
    })),
    h('input.input.input-sm', {
      type: 'number', min: 0, step: 10, value: recipe.max_time_points ?? 0,
      style: { width: '84px' },
      oninput: (e) => { recipe.max_time_points = Math.max(0, Number(e.target.value) || 0); },
    }),
    h('span.xsmall.dim', '0 = 全部帧'));
}
