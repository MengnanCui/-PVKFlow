// 数据处理 —— 这一期的核心页面。
// 左：来源  中：工作台  右：助手
//
// 中间那栏的参数表单和结果卡片都是从 skill 的 spec 自动生成的，
// 没有为任何一个具体 skill 写过界面代码。

import { api } from '../api.js';
import {
  h, mount, clear, toast, empty, skeletonRows, errorBox, busy, modal,
  fmtBytes, fmtNum, fmtTime,
} from '../ui.js';
import { xyChart } from '../chart.js';
import { paramForm } from '../components/form.js';
import { openImportDialog, bindDropUpload } from '../components/filepicker.js';
import { statusClass, statusText } from './overview.js';

export const meta = {
  id: 'process',
  title: '数据处理',
  desc: '导入 → 选文件 → 选处理 → 运行 → 结果回写',
};

const state = {
  files: [], total: 0, query: '', selected: null,
  skills: [], suggestions: [], activeSkill: null,
  preview: null, lastRun: null, assist: null, assistStatus: null,
  form: null,
};

let refs = {};

export function actions() {
  return [
    h('button.btn.btn-sm', { onclick: () => reloadSkills() }, '重载 Skill'),
    h('button.btn.btn-primary', { onclick: () => openImport() }, '导入数据'),
  ];
}

export async function view(host, ctx) {
  const left = h('section.panel');
  const mid = h('section.panel');
  const right = h('section.panel');
  mount(host, h('div.workbench', left, mid, right));

  refs = { left, mid, right, host };
  bindDropUpload(left, () => loadFiles());
  left.classList.add('drop-target');

  renderLeft();
  renderMid();
  renderRight();

  bindShortcuts();
  await Promise.all([loadFiles(), loadSkills(), loadAssistStatus()]);
}

// ------------------------------------------------------------------ 左栏
function renderLeft() {
  const listHost = h('div#fileListHost');
  mount(refs.left,
    h('div.panel-head',
      h('div.panel-title', '来源'),
      h('span.xsmall.dim#fileCount', '')),
    h('div', { style: { padding: 'var(--s3x) var(--s4x)', borderBottom: '1px solid var(--line)' } },
      h('input.input', {
        placeholder: '搜索文件名或路径…',
        value: state.query,
        oninput: debounce((e) => { state.query = e.target.value; loadFiles(); }, 220),
      }),
      h('div.row.gap-2.mt-3',
        h('button.btn.btn-sm.grow', { onclick: () => openImport() }, '浏览本机目录'),
        h('button.btn.btn-sm', {
          title: '检查引用型文件是否还在原位',
          onclick: async (e) => {
            busy(e.target, true);
            try {
              const r = await api.verifyFiles();
              toast(r.missing.length ? `${r.missing.length} 个引用文件已不在原位` : '所有引用文件都在',
                    r.missing.length ? 'err' : 'ok');
              loadFiles();
            } catch (err) { toast(err.message, 'err'); }
            busy(e.target, false);
          },
        }, '巡检')),
      h('p.xsmall.dim.mt-2', '也可以把文件拖到这一栏（拖拽通道会复制全部文件）')),
    h('div.panel-body.flush', listHost));
  refs.listHost = listHost;
}

async function loadFiles() {
  if (!refs.listHost) return;
  mount(refs.listHost, skeletonRows(6));
  try {
    const data = await api.listFiles({ q: state.query, limit: 300 });
    state.files = data.rows;
    state.total = data.total;
    const label = refs.left.querySelector('#fileCount');
    if (label) label.textContent = data.total ? `${data.rows.length} / ${data.total}` : '';
    drawFileList();
  } catch (err) {
    mount(refs.listHost, errorBox(err, loadFiles));
  }
}

function drawFileList() {
  if (!state.files.length) {
    mount(refs.listHost, empty(
      state.query ? '没有匹配的文件' : '还没有导入数据',
      state.query ? null : h('button.btn.btn-sm', { onclick: () => openImport() }, '导入数据')));
    return;
  }
  mount(refs.listHost, h('div.list', ...state.files.map((f) =>
    h('button.list-row', {
      'aria-selected': String(state.selected?.artifact_id === f.artifact_id),
      onclick: () => selectFile(f),
      dataset: { id: f.artifact_id },
    },
      f.thumb_path ? h('img.list-thumb', { src: api.thumbUrl(f.artifact_id), alt: '', loading: 'lazy' }) : null,
      h('div.grow',
        h('div.name.truncate', { title: f.display_path }, f.filename),
        h('div.meta.truncate',
          [f.sample_name, fmtBytes(f.size),
           f.storage_mode === 'referenced' ? '原地引用' : null,
          ].filter(Boolean).join(' · '))),
      f.status === 'missing' ? h('span.tag.tag-warn', '丢失') : null))));
}

async function selectFile(f) {
  state.selected = f;
  state.lastRun = null;
  state.preview = null;
  drawFileList();
  renderMid();
  renderRight();

  const [preview, suggestion] = await Promise.allSettled([
    api.preview(f.artifact_id, 400),
    api.suggest([f.artifact_id]),
  ]);
  state.preview = preview.status === 'fulfilled' ? preview.value : { error: preview.reason };
  state.suggestions = suggestion.status === 'fulfilled' ? suggestion.value.suggestions : [];
  if (state.suggestions.length) {
    const best = state.suggestions.find((s) => s.ready) || state.suggestions[0];
    state.activeSkill = state.skills.find((s) => s.id === best.skill_id) || null;
  }
  renderMid();
  loadAssist();
}

// ------------------------------------------------------------------ 中栏
function renderMid() {
  if (!state.selected) {
    mount(refs.mid,
      h('div.panel-head', h('div.panel-title', '工作台')),
      h('div.panel-body', empty('从左边选一个文件开始')));
    return;
  }

  const f = state.selected;
  mount(refs.mid,
    h('div.panel-head',
      h('div.grow',
        h('div.panel-title.truncate', { title: f.display_path }, f.filename),
        h('div.xsmall.dim.truncate', f.display_path)),
      h('button.btn.btn-ghost.btn-sm', { onclick: () => showRawText(f) }, '看原文')),
    h('div.panel-body#midBody'));

  const body = refs.mid.querySelector('#midBody');
  mount(body,
    h('div#previewHost'),
    h('div.divider'),
    h('div#skillHost'),
    h('div#resultHost'));

  drawPreview(body.querySelector('#previewHost'));
  drawSkillPicker(body.querySelector('#skillHost'));
  drawResult(body.querySelector('#resultHost'));
}

function drawPreview(host) {
  const p = state.preview;
  if (!p) { mount(host, skeletonRows(4)); return; }
  if (p.error) { mount(host, errorBox(p.error)); return; }

  if (p.kind === 'image') {
    mount(host,
      h('img', {
        src: p.has_thumb ? api.thumbUrl(state.selected.artifact_id) : api.rawUrl(state.selected.artifact_id),
        alt: state.selected.filename,
        style: { maxWidth: '100%', borderRadius: 'var(--r)', border: '1px solid var(--line)' },
      }),
      h('p.xsmall.dim.mt-2',
        `${p.meta?.width || '?'} × ${p.meta?.height || '?'} px · 原图保持在本机原位置，未复制`));
    return;
  }
  if (p.kind === 'text') {
    mount(host,
      p.note ? h('div.notice.notice-warn', h('div.grow', p.note)) : null,
      h('pre.mono.small', {
        style: { marginTop: '8px', maxHeight: '240px', overflow: 'auto',
                 background: 'var(--surface-2)', padding: 'var(--s3x)',
                 borderRadius: 'var(--r)', whiteSpace: 'pre-wrap' },
      }, p.text));
    return;
  }

  // 表格：先画图（数值列够的话），再给表格预览
  const numericCols = Object.entries(p.dtypes || {})
    .filter(([, t]) => t === 'numeric').map(([c]) => c);

  const nodes = [];
  if (numericCols.length >= 2) {
    const xi = p.columns.indexOf(numericCols[0]);
    const spec = {
      x_label: numericCols[0],
      y_label: numericCols[1],
      series: numericCols.slice(1, 4).map((col) => {
        const yi = p.columns.indexOf(col);
        const x = [], y = [];
        for (const row of p.rows) {
          const xv = Number(row[xi]), yv = Number(row[yi]);
          x.push(Number.isFinite(xv) ? xv : null);
          y.push(Number.isFinite(yv) ? yv : null);
        }
        return { label: col, x, y, style: 'line' };
      }),
    };
    nodes.push(xyChart(spec, { height: 260 }));
    nodes.push(h('p.xsmall.dim', '拖动可框选放大，双击还原。这是原始数据预览，不是分析结果。'));
  }

  nodes.push(h('details', { style: { marginTop: 'var(--s3x)' } },
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      `表格预览（${p.n_rows} 行 × ${p.columns.length} 列）`),
    h('div.table-wrap.mt-2', { style: { maxHeight: '260px', border: '1px solid var(--line)',
                                        borderRadius: 'var(--r)' } },
      h('table.data',
        h('thead', h('tr', ...p.columns.map((c) =>
          h('th' + (p.dtypes[c] === 'numeric' ? '.num' : ''), c)))),
        h('tbody', ...p.rows.slice(0, 120).map((row) =>
          h('tr', ...row.map((v, i) =>
            h('td' + (p.dtypes[p.columns[i]] === 'numeric' ? '.num' : ''),
              v === null ? h('span.dim', '—') : fmtNum(v))))))))));

  if (p.sniffed?.delimiter !== undefined) {
    nodes.push(h('p.xsmall.dim.mt-2',
      `识别结果：编码 ${p.sniffed.encoding} · 分隔符 ${JSON.stringify(p.sniffed.delimiter)}` +
      (p.sniffed.preamble?.length ? ` · 跳过 ${p.sniffed.preamble.length} 行抬头` : '')));
  }
  mount(host, ...nodes);
}

function drawSkillPicker(host) {
  const suggested = state.suggestions;
  const all = state.skills;
  if (!all.length) { mount(host, skeletonRows(2)); return; }

  const ranked = [...all].sort((a, b) => {
    const sa = suggested.find((s) => s.skill_id === a.id)?.score ?? -1;
    const sb = suggested.find((s) => s.skill_id === b.id)?.score ?? -1;
    return sb - sa;
  });
  if (!state.activeSkill) state.activeSkill = ranked[0];

  const tabs = h('div.row.wrap.gap-2', ...ranked.map((s) => {
    const score = suggested.find((x) => x.skill_id === s.id)?.score;
    const active = state.activeSkill?.id === s.id;
    return h('button.btn.btn-sm', {
      style: active ? { borderColor: 'var(--accent)', color: 'var(--accent)',
                        background: 'var(--accent-wash)' } : null,
      onclick: () => { state.activeSkill = s; renderMid(); },
      title: s.description,
    }, s.name,
      score >= 0.6 ? h('span.xsmall', { style: { color: 'var(--accent)' } }, '推荐') : null,
      !s.ready ? h('span.xsmall.dim', '待接入') : null);
  }));

  const skill = state.activeSkill;
  const columns = state.preview?.columns || [];
  state.form = paramForm(skill.params, {}, { columns });

  mount(host,
    h('div.row-between',
      h('div.section-title', '处理'),
      h('span.xsmall.dim', `${skill.id} · v${skill.version}` +
        (skill.origin === 'user' ? ' · 你添加的' : skill.origin === 'skill.md' ? ' · SKILL.md' : ''))),
    skill.description ? h('p.small.muted.mt-2', skill.description) : null,

    !skill.ready
      ? h('div.notice.notice-warn.mt-3',
          h('div.grow',
            h('div.strong', '这个处理还没有接入算法'),
            h('p.small.mt-2', skill.ready_note || '契约已就位，算法待填。')))
      : null,

    h('div.mt-3', state.form.node),

    skill.outputs?.length
      ? h('p.xsmall.dim.mt-3',
          '将产出：' + skill.outputs.map((o) => o.label + (o.unit ? `(${o.unit})` : '')).join('、'))
      : null,

    h('div.row.gap-2.mt-4',
      h('button.btn.btn-primary', {
        disabled: !skill.ready,
        onclick: (e) => runSkill(e.target, true),
      }, '运行并保存'),
      h('button.btn', {
        disabled: !skill.ready,
        title: '只跑一遍看结果，不写数据库',
        onclick: (e) => runSkill(e.target, false),
      }, '试跑'),
      h('span.xsmall.dim', { style: { marginLeft: 'auto' } },
        skill.ready ? '保存会生成 analysis_run_id 并记录算法版本' : '')));
}

async function runSkill(button, save) {
  const skill = state.activeSkill;
  if (!skill || !state.selected) return;
  busy(button, true);
  try {
    const out = await api.run(skill.id, [state.selected.artifact_id], state.form.get(), save);
    state.lastRun = out;
    toast(save
      ? `已保存 ${out.metrics_written} 条关键结果`
      : '试跑完成，没有写入数据库', 'ok');
    drawResult(refs.mid.querySelector('#resultHost'));
    refs.mid.querySelector('#resultHost')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    state.lastRun = { error: err };
    drawResult(refs.mid.querySelector('#resultHost'));
    toast(err.message, 'err', 7000);
  }
  busy(button, false);
}

function drawResult(host) {
  if (!host) return;
  const r = state.lastRun;
  if (!r) { clear(host); return; }
  if (r.error) {
    mount(host, h('div.divider'), errorBox(r.error));
    return;
  }

  mount(host,
    h('div.divider'),
    h('div.row-between',
      h('div.section-title', '结果'),
      h('div.row.gap-2',
        r.saved
          ? h('span.status.status-ok.xsmall', `已写入 ${r.metrics_written} 条`)
          : h('span.status.status-idle.xsmall', '试跑，未写入'),
        r.analysis_run_id ? h('span.mono.xsmall.dim', r.analysis_run_id) : null)),

    r.summary ? h('p.small.mt-2', r.summary) : null,

    r.warnings?.length
      ? h('div.notice.notice-warn.mt-3',
          h('div.grow', ...r.warnings.map((w) => h('div.small', w))))
      : null,

    r.metrics?.length
      ? h('div.result-grid.mt-3', ...r.metrics.slice(0, 24).map((m) => {
          const isText = typeof m.value === 'string';
          return h('div.result-item',
            h('div.k.truncate', { title: m.field_name }, m.label || m.field_name),
            h('div.v' + (isText ? '.is-text' : ''), { title: String(m.value ?? '') },
              fmtNum(m.value), m.unit ? h('span.unit', m.unit) : null));
        }))
      : h('p.small.muted.mt-3', '这次处理没有产出关键结果。'),

    r.preview?.series?.length
      ? h('div.mt-4',
          h('div.section-title.small', '分析输出'),
          xyChart(r.preview, { height: 280 }))
      : null,

    r.tables?.length
      ? h('p.xsmall.dim.mt-3',
          '数值表已存为 Parquet：' + r.tables.map((t) => `${t.name} (${t.n_rows} 行)`).join('、'))
      : null);
}

async function showRawText(f) {
  const body = h('div', skeletonRows(6));
  modal({ title: f.filename, width: '760px', body });
  try {
    const text = await api.headText(f.artifact_id, 120);
    mount(body, h('pre.mono.small', {
      style: { margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
    }, text));
  } catch (err) {
    mount(body, errorBox(err));
  }
}

// ------------------------------------------------------------------ 右栏
function renderRight() {
  mount(refs.right,
    h('div.panel-head',
      h('div.panel-title', '助手'),
      h('span#assistBadge')),
    h('div.panel-body#assistBody'));
  drawAssistBadge();
  drawAssist();
}

async function loadAssistStatus() {
  try {
    state.assistStatus = await api.assistStatus();
  } catch { state.assistStatus = { model_configured: false }; }
  drawAssistBadge();
  drawAssist();
}

function drawAssistBadge() {
  const el = refs.right?.querySelector('#assistBadge');
  if (!el) return;
  const s = state.assistStatus;
  mount(el, s?.model_configured
    ? h('span.status.status-accent.xsmall', s.active ? s.active.model : '已配置模型')
    : h('span.status.status-idle.xsmall', '规则模式'));
}

async function loadAssist() {
  if (!state.selected) return;
  state.assist = null;
  drawAssist();
  try {
    state.assist = await api.inspect([state.selected.artifact_id]);
  } catch (err) {
    state.assist = { error: err };
  }
  drawAssist();
}

function drawAssist() {
  const body = refs.right?.querySelector('#assistBody');
  if (!body) return;

  if (!state.selected) {
    mount(body, empty('选中文件后，这里会给出识别结果与建议'));
    return;
  }
  if (!state.assist) { mount(body, skeletonRows(5)); return; }
  if (state.assist.error) { mount(body, errorBox(state.assist.error, loadAssist)); return; }

  const a = state.assist;
  const file = a.files?.[0] || {};
  const issues = a.issues || [];
  const hasModel = state.assistStatus?.model_configured;

  mount(body,
    h('div',
      h('div.row-between',
        h('span.small.strong', '识别结果'),
        h('span.xsmall.dim', '规则')),
      h('div.mt-2.small.muted',
        h('div', `类型：${kindText(file.kind)}` + (file.domain ? ` · 疑似 ${file.domain}` : '')),
        file.columns ? h('div.truncate', { title: file.columns.join(', ') },
                         `列：${file.columns.join('、')}`) : null,
        file.encoding ? h('div', `编码 ${file.encoding} · 分隔符 ${JSON.stringify(file.delimiter)}`) : null)),

    h('div.divider'),
    h('div',
      h('div.row-between',
        h('span.small.strong', '候选处理'),
        h('span.xsmall.dim', '按匹配度排序')),
      a.suggestions?.length
        ? h('div.col.gap-2.mt-2', ...a.suggestions.slice(0, 4).map((s) =>
            h('button.btn.btn-sm', {
              style: { justifyContent: 'space-between', width: '100%' },
              onclick: () => {
                state.activeSkill = state.skills.find((k) => k.id === s.skill_id);
                renderMid();
              },
            }, h('span', s.name, s.ready ? null : h('span.xsmall.dim', ' 待接入')),
               h('span.xsmall.dim.mono', s.score.toFixed(2))))) 
        : h('p.small.muted.mt-2', '没有匹配的处理工具')),

    issues.length ? h('div.divider') : null,
    issues.length
      ? h('div',
          h('div.row-between',
            h('span.small.strong', '数据检查'),
            h('span.xsmall.dim', '规则')),
          h('div.col.gap-2.mt-2', ...issues.slice(0, 6).map((i) =>
            h('div.notice' + (i.level === 'warn' ? '.notice-warn' : i.level === 'error' ? '.notice-danger' : ''),
              h('div.grow',
                h('div.small', i.message),
                i.detail ? h('div.xsmall.dim.mt-2', i.detail) : null)))))
      : null,

    h('div.divider'),
    h('div',
      h('div.row-between',
        h('span.small.strong', '问模型'),
        hasModel ? h('span.xsmall.dim', state.assistStatus.active?.model || '') : null),
      hasModel
        ? h('div.mt-2',
            h('textarea.textarea#askBox', {
              placeholder: '例如：这份数据的分隔符识别对吗？异常点集中在哪一段？',
            }),
            h('div.row.gap-2.mt-2',
              h('button.btn.btn-primary.btn-sm', { onclick: (e) => ask(e.target) }, '提问'),
              h('span.xsmall.dim', '模型只能看到上面这些规则结论，不会编数据')),
            h('div#answerHost'))
        : h('div.notice.mt-2',
            h('div.grow',
              h('div.small', '还没有配置模型。'),
              h('p.xsmall.dim.mt-2',
                '上面的识别、候选处理、数据检查全部来自规则引擎，不依赖模型，现在就是可用的。'),
              h('div.mt-2', h('button.btn.btn-sm', {
                onclick: () => window.dispatchEvent(new CustomEvent('hte:nav', { detail: 'settings' })),
              }, '去配置'))))));
}

async function ask(button) {
  const box = refs.right.querySelector('#askBox');
  const host = refs.right.querySelector('#answerHost');
  const q = box.value.trim();
  if (!q) return;
  busy(button, true);
  mount(host, h('div.mt-3', skeletonRows(3)));
  try {
    const r = await api.ask(q, state.selected ? [state.selected.artifact_id] : [],
                            state.lastRun ? {
                              skill: state.lastRun.skill?.id,
                              metrics: state.lastRun.metrics,
                              summary: state.lastRun.summary,
                            } : null);
    mount(host,
      h('div.notice.notice-accent.mt-3',
        h('div.grow',
          h('div.xsmall.dim', `${r.provider} / ${r.model}`),
          h('div.small.mt-2', { style: { whiteSpace: 'pre-wrap' } }, r.answer))));
  } catch (err) {
    mount(host, h('div.mt-3', errorBox(err)));
  }
  busy(button, false);
}

// ------------------------------------------------------------------ 其它
async function loadSkills() {
  try {
    const data = await api.skills();
    state.skills = data.skills;
    if (data.errors?.length) {
      toast(`${data.errors.length} 个 skill 加载失败，详见设置页`, 'err', 6000);
    }
  } catch (err) { toast(err.message, 'err'); }
  if (state.selected) renderMid();
}

async function reloadSkills() {
  try {
    const r = await api.reloadSkills();
    state.skills = r.skills;
    toast(`已重新加载 ${r.count} 个 skill` + (r.errors.length ? `，${r.errors.length} 个失败` : ''),
          r.errors.length ? 'err' : 'ok');
    renderMid();
  } catch (err) { toast(err.message, 'err'); }
}

function openImport() {
  openImportDialog({ onDone: () => loadFiles() });
}

function bindShortcuts() {
  if (bindShortcuts.done) return;
  bindShortcuts.done = true;
  document.addEventListener('keydown', (e) => {
    if (!document.querySelector('#page-process.is-active')) return;
    if (e.target.matches('input, textarea, select')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') { e.preventDefault(); openImport(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const i = state.files.findIndex((f) => f.artifact_id === state.selected?.artifact_id);
      const next = state.files[Math.max(0, Math.min(state.files.length - 1,
                                                    i + (e.key === 'ArrowDown' ? 1 : -1)))];
      if (next && next !== state.selected) { e.preventDefault(); selectFile(next); }
    }
  });
}

const kindText = (k) => ({ table: '表格数据', image: '图像', text: '纯文本' }[k] || '未识别');

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
