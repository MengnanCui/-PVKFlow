// 数据处理页（process.js）的页面状态。
//
// S 是模块级的：离开页面再回来还是同一个对象。refs 是「这一次进入页面」的东西
// （容器节点、导航、alive()），每次进入时整体换一份。

export const S = {
  fn: 'abs-thickness',
  filter: {},
  facets: null,
  rows: [],          // 已加载的页
  total: 0,
  loading: false,
  checked: new Set(),   // 手工勾选的 sample_id
  anchor: null,         // 上次点选的行号，shift 范围选从这儿起
  sets: [],
};

export const refs = {};

/** 整体换一份（ES 模块的导入方不能给 `refs` 赋值）。 */
export function resetRefs(next) {
  for (const k of Object.keys(refs)) delete refs[k];
  Object.assign(refs, next);
}

/**
 * 拆出去的文件要回头调页面入口里的几个函数（drawList、reload、syncSelection）。
 * 直接 import 入口会成环（离线烤制要求依赖无环），所以由入口在加载时登记在这里。
 */
export const hooks = {};
