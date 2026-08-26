// 数据处理 —— 功能切换 + 样品列表。
//
// 压成两层：从这里到看见图只需一次点击。功能层只有 4 项、现在只有 1 项可用，
// 单独占一次页面跳转不划算，所以做成顶部 tab。

import { api } from '../api.js';
import {
  h, mount, toast, empty, skeletonRows, errorBox, busy, fmtBytes, fmtInt, fmtTime,
} from '../ui.js';
import { openImportDialog, bindDropUpload } from '../components/filepicker.js';

export const meta = {
  id: 'process',
  title: '数据处理',
  desc: '选一个功能，再选一个样品',
};

// 四个子功能。ready=false 的如实标注，不做假入口。
const FUNCTIONS = [
  {
    id: 'abs-thickness', name: '吸收光谱 & 膜厚', ready: true,
    desc: '原位吸收光谱矩阵：强度热力图、干涉条纹、光学膜厚、谱斜率与波段积分',
  },
  { id: 'pl-pl', name: '荧光 & 荧光', ready: false, desc: '双通道荧光关联' },
  { id: 'pl-scatter', name: '荧光 & 散射', ready: false, desc: '荧光与散射同步采集' },
  { id: 'qfls', name: 'QFLS', ready: false, desc: '准费米能级分裂' },
];

const state = { fn: 'abs-thickness', query: '', data: null, error: null };
let refs = {};

export function actions(nav) {
  return [
    h('button.btn.btn-primary', { onclick: () => openImport() }, '导入数据'),
  ];
}

export async function view(host, ctx) {
  const body = h('div');
  mount(host, h('div.section', functionTabs()), body);
  refs = { host, body, nav: ctx.nav };
  bindDropUpload(host, () => load());
  await load();
}

function functionTabs() {
  return h('div.fn-tabs',
    ...FUNCTIONS.map((f) => h('button.fn-tab', {
      'aria-selected': String(state.fn === f.id),
      disabled: !f.ready,
      title: f.ready ? f.desc : '规划中',
      onclick: () => { if (f.ready) { state.fn = f.id; rerender(); } },
    },
      h('span.fn-tab-name', f.name),
      h('span.fn-tab-desc', f.ready ? f.desc : '规划中'))));
}

function rerender() {
  mount(refs.host, h('div.section', functionTabs()), refs.body);
  load();
}

async function load() {
  const fn = FUNCTIONS.find((f) => f.id === state.fn);
  if (!fn.ready) {
    mount(refs.body, h('div.panel', empty(`${fn.name} 还没有开始做`)));
    return;
  }
  mount(refs.body, skeletonRows(6));
  try {
    state.data = await api.spectraSamples();
    state.error = null;
  } catch (err) {
    state.error = err;
  }
  drawList();
}

function drawList() {
  if (state.error) { mount(refs.body, errorBox(state.error, load)); return; }

  const all = state.data.samples;
  const q = state.query.trim().toLowerCase();
  const rows = q
    ? all.filter((s) => (s.name + ' ' + (s.batch || '')).toLowerCase().includes(q))
    : all;
  const withMatrix = rows.filter((s) => s.matrices.length);
  const without = rows.filter((s) => !s.matrices.length);

  mount(refs.body,
    h('div.section',
      h('div.section-head',
        h('div.row.gap-3',
          h('input.input', {
            placeholder: '搜索样品 / 批次…',
            value: state.query,
            style: { width: '220px' },
            oninput: (e) => { state.query = e.target.value; drawList(); },
          }),
          h('span.small.muted',
            state.data.with_matrix
              ? `${state.data.with_matrix} 个样品有光谱矩阵`
              : '还没有找到光谱矩阵')),
        h('button.btn.btn-sm', { onclick: () => openImport() }, '浏览本机目录')),

      withMatrix.length
        ? h('div.panel.panel-body.flush', sampleTable(withMatrix))
        : h('div.panel', empty(
            all.length
              ? '导入的文件里没有找到光谱矩阵（宽表：一列一个时刻）'
              : '还没有导入数据',
            h('button.btn.btn-sm', { onclick: () => openImport() }, '导入数据'))),

      without.length
        ? h('details.mt-4',
            h('summary.small.muted', { style: { cursor: 'pointer' } },
              `另有 ${without.length} 个样品没有光谱矩阵`),
            h('div.panel.panel-body.flush.mt-2',
              h('table.data',
                h('tbody', ...without.map((s) => h('tr',
                  h('td.strong', s.name),
                  h('td.small.muted', s.batch || '—'),
                  h('td.small.muted', `${s.other_files} 个其它文件`)))))))
        : null));
}

function sampleTable(rows) {
  return h('table.data',
    h('thead', h('tr',
      h('th', '样品'), h('th', '批次'), h('th', '光谱矩阵'),
      h('th.num', '大小'), h('th', ''))),
    h('tbody', ...rows.map((s) => {
      const m = s.matrices[0];
      const missing = m.status === 'missing';
      const open = () => {
        if (missing) { toast('这个文件在原位置找不到了', 'err'); return; }
        refs.nav('sample', { arg: m.artifact_id });
      };
      return h('tr', {
        style: { cursor: missing ? 'not-allowed' : 'pointer' },
        onclick: open,
      },
        h('td', h('span.strong', s.name),
          s.matrices.length > 1
            ? h('div.xsmall.dim', `还有 ${s.matrices.length - 1} 个矩阵文件`) : null),
        h('td.small.muted', s.batch || '—'),
        h('td',
          h('div.truncate', { style: { maxWidth: '340px' }, title: m.display_path },
            m.filename),
          m.columns_hint ? h('div.xsmall.dim', `${fmtInt(m.columns_hint)} 列`) : null),
        h('td.num.small.muted', fmtBytes(m.size)),
        h('td', { style: { width: '90px', textAlign: 'right' } },
          missing
            ? h('span.status.status-warn.xsmall', '文件丢失')
            : h('button.btn.btn-sm', { onclick: (e) => { e.stopPropagation(); open(); } },
                '打开')));
    })));
}

function openImport() {
  openImportDialog({ onDone: () => load() });
}
