// 后端调用。所有错误都被翻译成带 message / kind / detail 的 Error，
// 界面照实显示，不吞掉。

class ApiError extends Error {
  constructor(message, kind, detail, status) {
    super(message);
    this.kind = kind; this.detail = detail; this.status = status;
  }
}

async function request(path, { method = 'GET', body, signal, raw = false } = {}) {
  const init = { method, signal, headers: {} };
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
    if (!res.ok) throw new ApiError(`请求失败（${res.status}）`, 'http', await res.text(), res.status);
    return res;
  }

  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

  if (!res.ok) {
    const e = data?.error || {};
    throw new ApiError(e.message || `请求失败（${res.status}）`, e.kind || 'http',
                       e.detail || text.slice(0, 500), res.status);
  }
  return data;
}

const get  = (p, q) => request(p + (q ? '?' + new URLSearchParams(
  Object.entries(q).filter(([, v]) => v !== undefined && v !== null && v !== '')) : ''));
const post = (p, body) => request(p, { method: 'POST', body });

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
  listFiles: (params) => get('/api/files', params),
  fileFacets: () => get('/api/files/facets'),
  fileDetail: (id) => get(`/api/files/${id}`),
  verifyFiles: () => post('/api/files/verify'),
  namingPreview: (paths, rules) => post('/api/files/naming/preview', { paths, rules }),

  // artifact 内容
  preview: (id, rows = 400) => get(`/api/artifacts/${id}/preview`, { rows }),
  headText: (id, lines = 60) => request(`/api/artifacts/${id}/head?lines=${lines}`, { raw: true })
                                  .then((r) => r.text()),
  rawUrl: (id) => `/api/artifacts/${id}/raw`,
  thumbUrl: (id) => `/api/artifacts/${id}/thumb`,

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

  // skill
  skills: () => get('/api/skills'),
  reloadSkills: () => post('/api/skills/reload'),
  suggest: (artifactIds) => post('/api/skills/suggest', { artifact_ids: artifactIds }),
  run: (skillId, artifactIds, params, save = true) =>
    post('/api/skills/run', { skill_id: skillId, artifact_ids: artifactIds, params, save }),
  recentRuns: (limit = 50) => get('/api/skills/runs/recent', { limit }),
  runDetail: (id) => get(`/api/runs/${id}`),

  // 结果
  results: (params) => get('/api/results', params),
  fields: () => get('/api/results/fields'),
  setQuality: (id, quality) => post(`/api/results/${id}/quality`, { quality }),
  table: (id, limit) => get(`/api/tables/${id}`, { limit }),
  samples: () => get('/api/samples'),
  storageStats: () => get('/api/storage/stats'),

  // 选择：传的是筛选式，不是 ID 列表
  selectionQuery: (body) => post('/api/selection/query', body),
  selectionCount: (filter) => post('/api/selection/count', { filter }),
  selectionFacets: (body) => post('/api/selection/facets', body),
  selectionIds: (filter, limit) => post('/api/selection/ids', { filter, limit }),
  suggestExpansion: (sampleIds, filter) =>
    post('/api/selection/suggest', { sample_ids: sampleIds, filter }),
  listSets: () => get('/api/selection/sets'),
  createSet: (body) => post('/api/selection/sets', body),
  getSet: (id) => get(`/api/selection/sets/${id}`),
  freezeSet: (id) => post(`/api/selection/sets/${id}/freeze`),
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

  // 光谱矩阵
  spectraSamples: () => get('/api/spectra/samples'),
  spectraMeta: (id) => get(`/api/spectra/${id}/meta`),
  spectraFrames: (id, params) => get(`/api/spectra/${id}/frames`, params),
  spectraFramesBin: async (dataUrl) => {
    const res = await request(dataUrl, { raw: true });
    return new Float32Array(await res.arrayBuffer());
  },
  heatmapUrl: (id, params) => `/api/spectra/${id}/heatmap.png?` + new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')),
  spectraCurve: (id, params) => get(`/api/spectra/${id}/curve`, params),
  spectraThickness: (id, params) => get(`/api/spectra/${id}/thickness`, params),

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
  sendMessage: async (id, body, { signal, onFrame } = {}) => {
    const res = await request(`/api/chat/conversations/${id}/messages`,
                              { method: 'POST', body, signal, raw: true });
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
  },

  // 助手（单文件页里那个规则引擎面板，跟抽屉是两回事）
  assistStatus: () => get('/api/assist/status'),
  inspect: (artifactIds) => post('/api/assist/inspect', { artifact_ids: artifactIds }),
  ask: (question, artifactIds, resultContext) =>
    post('/api/assist/ask', { question, artifact_ids: artifactIds, result_context: resultContext }),

  // 设置
  settings: () => get('/api/settings'),
  saveSettings: (patch) => post('/api/settings', patch),
  models: () => get('/api/settings/models'),
  saveModels: (config) => post('/api/settings/models', { config }),
  saveSimpleModel: (body) => post('/api/settings/models/simple', body),
  testModel: (provider, model) => post('/api/settings/models/test', { provider, model }),
  // 按地址拉一份可用模型。密钥留空 = 沿用已经存着的那个（返回里绝不带密钥）
  discoverModels: (body) => post('/api/settings/models/discover', body),
  modelExample: () => get('/api/settings/models/example'),
};

export { ApiError };
