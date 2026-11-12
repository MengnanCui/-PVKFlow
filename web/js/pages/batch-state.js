// 批处理结果页（batch.js）的页面状态，和几个文件都要用的小工具。
//
// S 是模块级的：离开页面再回来还是同一个对象，所以每次进入页面时 view() 先把它清空。
// 异步请求回来之后改 S 之前，要先问 refs.alive() —— 这一次进入页面还算不算数。

export const S = {
  runId: null, taskId: null, detail: null, error: null,
  pins: [],          // 钉在这次对比上的 AI 分析
  // 特殊处理的两条曲线 + 膜厚曲线
  curves: { integral: null, slope: null, ot: null },
  extra: [],         // 同事模块声明的、能画的批处理曲线（来自 detail.curves）
  mode: 'auto', groupBy: 'batch',
  modules: null, moduleErr: null, axes: null,   // 参数面板照模块声明生成
  // 时刻切片：窗口是**查询参数**，改一下不用重跑（膜厚曲线整条都存着了）
  win2: null,            // 第二张图的窗口。要先知道数据测到哪儿才定得下来
  win2Resolved: false,   // 已经按实际时间范围定过一次默认值了
  tMax: null,            // 这批数据里最晚的一帧
  slices: null, sliceErr: null,
};

export const refs = {};

/** 整体换一份（ES 模块的导入方不能给 `refs` 赋值）。 */
export function resetRefs(next) {
  for (const k of Object.keys(refs)) delete refs[k];
  Object.assign(refs, next);
}

export const batchCount = (series) => new Set(series.map((s) => s.group || '')).size;

export const Y_LABEL = { integral: '积分强度 (a.u.·nm)', slope: 'dI/dλ (a.u./nm)',
                         ot: '光学厚度 OT = n·d·cosθ (nm)' };

/** 平台那三条用写好的标签；同事模块的曲线用它自己声明的。 */
export function yLabel(col) {
  return Y_LABEL[col] || (S.extra || []).find((c) => c.column === col)?.y_label || col;
}

