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

export const api = {
  health: () => get('/api/health'),
  overview: () => get('/api/overview'),

  // 文件
  roots: () => get('/api/files/roots'),
  browse: (path, showHidden = false) => get('/api/files/browse', { path, show_hidden: showHidden }),
  scan: (path, recursive = true) => post('/api/files/scan', { path, recursive }),
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

  // 助手
  assistStatus: () => get('/api/assist/status'),
  inspect: (artifactIds) => post('/api/assist/inspect', { artifact_ids: artifactIds }),
  ask: (question, artifactIds, resultContext) =>
    post('/api/assist/ask', { question, artifact_ids: artifactIds, result_context: resultContext }),

  // 设置
  settings: () => get('/api/settings'),
  saveSettings: (patch) => post('/api/settings', patch),
  models: () => get('/api/settings/models'),
  saveModels: (config) => post('/api/settings/models', { config }),
  testModel: (provider, model) => post('/api/settings/models/test', { provider, model }),
  modelExample: () => get('/api/settings/models/example'),
};

export { ApiError };
