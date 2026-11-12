// 后端调用。所有错误都被翻译成带 message / kind / detail 的 Error，
// 界面照实显示，不吞掉。

class ApiError extends Error {
  constructor(message, kind, detail, status) {
    super(message);
    this.kind = kind; this.detail = detail; this.status = status;
  }
}

// 「这一次进入页面」的 signal（见 api.with）。request() 同步读它 —— 包装函数都是同步地调到 request 的，
// 所以 with() 设上、调完马上复原就够了，不会串到别的请求上。
let ambientSignal = null;

async function request(path, { method = 'GET', body, signal, raw = false } = {}) {
  const init = { method, signal: signal ?? ambientSignal ?? undefined, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError('连不上本地服务。是不是窗口被关掉了？', 'offline', String(err), 0);
  }

  if (raw) {
    if (!res.ok) {
      // 流式接口在开流之前报的错（没配模型 501、会话正忙 409）也是平台的错误结构，照样读出人话
      const text = await res.text();
      let e = {};
      try { e = JSON.parse(text)?.error || {}; } catch { /* 不是 JSON：原文当 detail */ }
      throw new ApiError(e.message || `请求失败（${res.status}）`, e.kind || 'http',
                         e.detail || (e.message ? '' : text.slice(0, 500)), res.status);
    }
    return res;
  }

  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

  if (!res.ok) {
    const e = data?.error || {};
    // ★ 原始响应体只在**看不懂**的时候才当 detail 用。
    //
    // 以前写的是 `e.detail || text.slice(0, 500)`：后端给的 detail 绝大多数
    // 是空串，于是每一个正常的报错都会在消息底下再糊一坨
    // `{"error":{"message":"没有这次批处理：run_x",…}}` —— 同一句话说两遍，
    // 第二遍还是给机器看的。
    // 真正需要原始文本的是另一种情况：返回的根本不是这个平台的错误结构
    // （公司代理的 HTML 登录页、网关自己的 502），那时候它是唯一的线索。
    throw new ApiError(e.message || `请求失败（${res.status}）`, e.kind || 'http',
                       e.detail || (e.message ? '' : text.slice(0, 500)), res.status);
  }
  return data;
}

const get  = (p, q, opts = {}) => request(p + (q ? '?' + new URLSearchParams(
  Object.entries(q).filter(([, v]) => v !== undefined && v !== null && v !== '')) : ''), opts);
const post = (p, body, opts = {}) => request(p, { method: 'POST', body, ...opts });

function parseFrame(block) {
  let event = null;
  let data = null;
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7).trim();
    else if (line.startsWith('data: ')) {
      try { data = JSON.parse(line.slice(6)); } catch { data = null; }
    }
  }
  return event ? { event, data: data || {} } : null;
}

/** POST 一个 SSE 接口，逐帧回调。出错（含开流之前的 501）照常抛 ApiError。 */
async function streamPost(path, body, { signal, onFrame } = {}) {
  const res = await request(path, { method: 'POST', body, signal, raw: true });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE 以空行分帧。最后一段可能是半截，留在缓冲里等下一块。
    const blocks = buf.split('\n\n');
    buf = blocks.pop();
    for (const block of blocks) {
      const frame = parseFrame(block);
      if (frame) onFrame?.(frame.event, frame.data);
    }
  }
}

export const api = {
  health: () => get('/api/health'),
  overview: () => get('/api/overview'),

  // 文件
  roots: () => get('/api/files/roots'),
  browse: (path, showHidden = false) => get('/api/files/browse', { path, show_hidden: showHidden }),
  scan: (path, opts = {}) => post('/api/files/scan', { path, ...opts }),
  importFiles: (files, sourceHint) => post('/api/files/import', { files, source_hint: sourceHint }),
  upload: (fileList) => {
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f, f.webkitRelativePath || f.name);
    return request('/api/files/upload', { method: 'POST', body: fd });
  },
  verifyFiles: () => post('/api/files/verify'),
  namingPreview: (paths, rules) => post('/api/files/naming/preview', { paths, rules }),


  // 功能模块（同事放进 workspace/modules/ 的那些）
  modules: () => get('/api/modules'),
  reloadModules: () => post('/api/modules/reload'),
  validateModule: (id) => post('/api/modules/validate', { module_id: id }),
  // changed = 这次动过的控件 key。后端据此跳过不受影响的面板（见 ctx.needs）
  moduleCompute: (id, artifactId, params, changed = null) =>
    post(`/api/modules/${id}/compute`,
         { artifact_id: artifactId, params, ...(changed ? { changed } : {}) }),
  moduleExportUrl: (id) => `/api/modules/${id}/export`,
  uninstallModule: (id) => request(`/api/modules/${id}`, { method: 'DELETE' }),
  importModule: (file) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return request('/api/modules/import', { method: 'POST', body: fd });
  },


  fields: () => get('/api/results/fields'),
  // 构效关系：两个字段的散点 + 相关；按批次的箱线图。filter 和数据处理页同一个筛选式
  relationPairs: (body) => post('/api/relation/pairs', body),
  relationGroups: (body) => post('/api/relation/groups', body),
  // 审核：可信 / 待查 / 作废，带理由。返回这条和审核之后的当前值
  setQuality: (id, quality, note = '') => post(`/api/results/${id}/quality`, { quality, note }),
  setQualityMany: (ids, quality, note = '') => post('/api/results/quality', { ids, quality, note }),
  sampleResults: (sampleId) => get(`/api/results/by-sample/${encodeURIComponent(sampleId)}`),
  storageStats: () => get('/api/storage/stats'),

  // 选择：传的是筛选式，不是 ID 列表
  selectionQuery: (body) => post('/api/selection/query', body),
  selectionFacets: (body) => post('/api/selection/facets', body),
  // 目前没有页面在用：给「勾了几个样品 → 建议扩成一个筛选式」留着（process-run.js 开头写了原因）
  suggestExpansion: (sampleIds, filter) =>
    post('/api/selection/suggest', { sample_ids: sampleIds, filter }),
  listSets: () => get('/api/selection/sets'),
  createSet: (body) => post('/api/selection/sets', body),
  getSet: (id) => get(`/api/selection/sets/${id}`),
  freezeSet: (id) => post(`/api/selection/sets/${id}/freeze`),
  renameSet: (id, name) => post(`/api/selection/sets/${id}/rename`, { name }),
  deleteSet: (id) => request(`/api/selection/sets/${id}`, { method: 'DELETE' }),

  // 批处理
  batchPreview: (filter) => post('/api/batch/preview', { filter }),
  batchRun: (body) => post('/api/batch/run', body),
  batchRuns: () => get('/api/batch/runs'),
  batchDetail: (id) => get(`/api/batch/runs/${id}`),
  batchCurves: (id, params) => get(`/api/batch/runs/${id}/curves`, params),
  // 这批数据的波长轴/时间轴范围。模块控件声明 range_from 时要用
  batchAxes: (id) => get(`/api/batch/runs/${id}/axes`),
  // 时刻切片：窗口写成 `0:1,27.5:28.5`。这是查询不是配方 —— 改窗口不用重跑
  batchSlices: (id, windows) =>
    get(`/api/batch/runs/${id}/slices`,
        { windows: windows.map(([a, b]) => `${a}:${b}`).join(',') }),
  batchExportUrl: (id, params) =>
    `/api/batch/runs/${id}/export?` + new URLSearchParams(params).toString(),
  batchExportPreview: (id, params) => get(`/api/batch/runs/${id}/export/preview`, params),

  // 后台任务
  listTasks: () => get('/api/tasks'),
  getTask: (id) => get(`/api/tasks/${id}`),
  cancelTask: (id) => post(`/api/tasks/${id}/cancel`),

  // 缓存
  cacheStatus: () => get('/api/settings/cache'),
  clearCache: () => post('/api/settings/cache/clear'),

  // 光谱矩阵（样品页）
  spectraMeta: (id) => get(`/api/spectra/${id}/meta`),
  spectraFrames: (id, params) => get(`/api/spectra/${id}/frames`, params),
  spectraFramesBin: async (dataUrl) => {
    const res = await request(dataUrl, { raw: true });
    return new Float32Array(await res.arrayBuffer());
  },
  heatmapUrl: (id, params) => `/api/spectra/${id}/heatmap.png?` + new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')),

  // AI 抽屉：会话、流式、钉住
  conversations: (q) => get('/api/chat/conversations', q),
  newConversation: (scope, title) => post('/api/chat/conversations', { scope, title }),
  conversation: (id) => get(`/api/chat/conversations/${id}`),
  patchConversation: (id, patch) =>
    request(`/api/chat/conversations/${id}`, { method: 'PATCH', body: patch }),
  deleteConversation: (id) => request(`/api/chat/conversations/${id}`, { method: 'DELETE' }),
  scopePreview: (scope) => post('/api/chat/scope/preview', { scope }),
  pins: (run) => get('/api/chat/pins', { run }),
  pinCounts: () => get('/api/chat/pins'),
  createPin: (body) => post('/api/chat/pins', body),
  deletePin: (id) => request(`/api/chat/pins/${id}`, { method: 'DELETE' }),

  /**
   * 发一条消息，逐帧回调。
   *
   * `raw: true` 拿到的是原始 Response —— api.js 里那条路本来就是为这个留的。
   * 注意顺序：**先用普通请求探一次 501**，再开流。raw 模式下 kind 被写死成
   * 'http'，「没配模型」的 no_model 会在那儿丢掉，界面就没法给出「去设置」
   * 这个恰当的出口了。
   */
  sendMessage: (id, body, opts = {}) => streamPost(`/api/chat/conversations/${id}/messages`, body, opts),

  // 智能体：发一句话 / 批完接着跑（都是 SSE）。没配模型时 501 在开流之前就回来
  agentTools: () => get('/api/agent/tools'),
  agentSettings: () => get('/api/agent/settings'),
  saveAgentSettings: (patch) => post('/api/agent/settings', patch),
  agentSessions: () => get('/api/agent/sessions'),
  agentSession: (id) => get(`/api/agent/sessions/${encodeURIComponent(id)}`),
  deleteAgentSession: (id) => request(`/api/agent/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  agentRun: (body, opts = {}) => streamPost('/api/agent/run', body, opts),
  agentResume: (id, body, opts = {}) => streamPost(`/api/agent/sessions/${encodeURIComponent(id)}/resume`, body, opts),
  agentStop: (id) => post(`/api/agent/sessions/${encodeURIComponent(id)}/stop`),
  agentUndoSession: (id) => post(`/api/agent/sessions/${encodeURIComponent(id)}/undo-files`),
  agentRollback: (changeId) => post(`/api/agent/file-changes/${encodeURIComponent(changeId)}/rollback`, {}),

  // 模型配好了没有（AI 抽屉和 ⓘ 用）
  assistStatus: () => get('/api/assist/status'),

  // 设置
  settings: () => get('/api/settings'),
  saveSettings: (patch) => post('/api/settings', patch),
  models: () => get('/api/settings/models'),
  saveModels: (config) => post('/api/settings/models', { config }),
  saveSimpleModel: (body) => post('/api/settings/models/simple', body),
  testModel: (provider, model) => post('/api/settings/models/test', { provider, model }),
  // 按地址拉一份可用模型。密钥留空 = 沿用已经存着的那个（返回里绝不带密钥）
  discoverModels: (body) => post('/api/settings/models/discover', body),
  // 给一个模型打一句「回复 ok」测延迟（设置页并发 3 个逐个测，可中途停）
  testOneModel: (body, signal) => request('/api/settings/models/test-one', { method: 'POST', body, signal }),
  // 地址候选：内置的公共网关 + 自己存的那几个（存在本机配置里，不进仓库）
  savePreset: (body) => post('/api/settings/models/presets', body),
  deletePreset: (baseUrl) => request(
    '/api/settings/models/presets?' + new URLSearchParams({ base_url: baseUrl }),
    { method: 'DELETE' }),
  modelExample: () => get('/api/settings/models/example'),

  // QFLS 成像（P-54 … P-58）：处理结果全在输出目录里，这些接口只是把它读出来
  qflsMethod: () => get('/api/qfls/method'),
  qflsScan: (root) => post('/api/qfls/scan', { root }),
  qflsRuns: () => get('/api/qfls/runs'),
  qflsStart: (body) => post('/api/qfls/runs', body),
  qflsRun: (id) => get(`/api/qfls/runs/${id}`),
  qflsPoints: (id) => get(`/api/qfls/runs/${id}/points`),
  qflsPoint: (id, key) => get(`/api/qfls/runs/${id}/points/${key}`),
  qflsDelete: (id) => request(`/api/qfls/runs/${id}`, { method: 'DELETE' }),
  qflsFile: (id, path) => `/api/qfls/runs/${id}/files/${path}`,
};

export { ApiError };

/**
 * 同一套接口，但这次发的请求都挂在 signal 上：离开页面时 signal 一断，还在路上的请求**真的中止**
 * （原来只能等它回来再丢掉 —— 大批处理的曲线、几十 MB 的光谱帧照样下载完，技术债 C076）。
 *
 *   const A = api.with(ctx.signal);
 *   const d = await A.batchDetail(id);     // 离开页面 → 抛 AbortError，调用方按「过期」处理
 *
 * 中止抛的是 AbortError（name === 'AbortError'）；ui.render 和各页的 alive() 判断都认它，不会显示成报错。
 */
api.with = (signal) => new Proxy(api, {
  get(target, key) {
    const f = target[key];
    if (typeof f !== 'function' || key === 'with' || !signal) return f;
    return (...args) => {
      const prev = ambientSignal;
      ambientSignal = signal;
      try { return f(...args); } finally { ambientSignal = prev; }
    };
  },
});
