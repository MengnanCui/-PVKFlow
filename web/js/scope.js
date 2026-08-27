// 「当前在看哪批样品」——数据处理页和 AI 抽屉之间唯一的共享状态。
//
// 抽屉需要知道你勾了几个、筛出了几个，但它不该去读 process.js 的私有 S。
// 这里是一个极小的发布/订阅：process 页写，抽屉读。
//
// 两档范围都表达成**筛选式**：「全部命中的」是分面面板那条，「选中的」是
// `{ids: [...]}`。后端一视同仁地编译成 SQL，整条链路上只有一种描述
// 「一批样品」的方式。
//
// 说清楚一件事：「选中的」这一档里 ID 列表是**你自己点出来的**，存下来天经地义。
// 「模型永远不返回 ID 列表」是另一回事 —— 那条规矩管的是模型的输出
// （见 app/api/chat.py 的 _card_from），不是你的手。

const state = {
  filter: {},      // 分面面板当前的筛选式
  total: 0,        // 该筛选式命中多少个
  checked: [],     // 手工勾选的 sample_id
  page: null,      // 谁发布的（'process' / 'batch' / …），只用于显示
  label: '',
};

const EVENT = 'hte:scope';

export function setScope(patch) {
  Object.assign(state, patch);
  window.dispatchEvent(new CustomEvent(EVENT, { detail: getScope() }));
}

export function getScope() {
  return { ...state, checked: [...state.checked] };
}

export function onScope(fn) {
  const h = (e) => fn(e.detail);
  window.addEventListener(EVENT, h);
  return () => window.removeEventListener(EVENT, h);
}

/** 范围 → 发给后端的 scope。两种模式走的是同一条查询路径，只是筛选式不同。 */
export function scopeFilter(mode) {
  if (mode === 'selected' && state.checked.length) {
    return { mode: 'selected', filter: { ids: [...state.checked] } };
  }
  return { mode: 'all', filter: { ...state.filter } };
}

/** 有没有手工勾选 —— 决定选择器默认停在哪一档。 */
export const hasSelection = () => state.checked.length > 0;
