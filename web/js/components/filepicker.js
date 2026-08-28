// 服务端目录浏览 + 导入预览。
//
// 为什么不是纯拖拽：浏览器出于安全不会把文件的真实路径给网页，
// 而「图像不复制、原地引用」必须知道真实路径。所以主通道是在应用里
// 浏览本机目录。拖拽仍然可用，但那条通道会把文件复制进工作区。

import { api } from '../api.js';
import { h, mount, modal, toast, fmtBytes, empty, skeletonRows, errorBox, busy } from '../ui.js';

// 最近导入过的目录。导数据是个反复动作 —— 每次都从「起始位置」一层层点回去
// 是纯粹的浪费。存 localStorage，跟工作区无关，换工作区也还在。
const RECENT_KEY = 'hte.import.recent';
const RECENT_MAX = 3;

function recentPaths() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY));
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, RECENT_MAX) : [];
  } catch { return []; }               // 隐私模式下 localStorage 会抛
}

function rememberPath(path) {
  if (!path) return;
  // 去重后放到最前面，只留 3 个
  const next = [path, ...recentPaths().filter((p) => p !== path)].slice(0, RECENT_MAX);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)); } catch { /* 同上 */ }
}

export async function openImportDialog({ onDone } = {}) {
  let cursor = null;
  const body = h('div');

  const m = modal({
    title: '导入实验数据',
    width: '860px',
    body,
    foot: [h('span.small.muted.grow',
             '文本类文件会复制进工作区，图像保持在原位只做登记'),
           h('button.btn', { onclick: () => m.close() }, '关闭')],
  });

  async function showRoots() {
    mount(body, skeletonRows(3));
    try {
      const { roots } = await api.roots();
      const recent = recentPaths();
      mount(body,
        h('p.small.muted', '选择一个目录，下一步会先给出扫描预览，确认后才写入。'),
        recent.length
          ? h('div.mt-3',
              h('div.small.strong.mb-2', '最近导入过'),
              h('div.panel',
                h('div.list', ...recent.map((rp) =>
                  h('button.list-row', { onclick: () => showDir(rp) },
                    h('span.muted', '↻'),
                    h('div.grow.min0',
                      h('div.name.truncate', rp.split(/[\\/]/).filter(Boolean).pop() || rp),
                      h('div.meta.mono.truncate', { title: rp }, rp)))))))
          : null,
        h('div.mt-4.small.strong.mb-2', '起始位置'),
        h('div.panel',
          h('div.list', ...roots.map((r) =>
            h('button.list-row', { onclick: () => showDir(r.path) },
              h('div.grow', h('div.name', r.name), h('div.meta.mono', r.path)))))),
        h('div.mt-3.field',
          h('label.field-label', '或者直接粘贴路径'),
          h('div.row.gap-2',
            h('input.input.mono#pastePath', { placeholder: 'D:\\实验数据\\2026-08' }),
            h('button.btn', {
              onclick: () => {
                const v = body.querySelector('#pastePath').value.trim();
                if (v) showDir(v);
              },
            }, '打开'))));
      body.querySelector('#pastePath')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); const v = e.target.value.trim(); if (v) showDir(v); }
      });
    } catch (err) {
      mount(body, errorBox(err, showRoots));
    }
  }

  async function showDir(path) {
    mount(body, skeletonRows(6));
    try {
      const data = await api.browse(path);
      cursor = data.path;
      mount(body,
        h('div.row.gap-2',
          h('button.btn.btn-sm', { onclick: showRoots }, '起始位置'),
          data.parent ? h('button.btn.btn-sm', { onclick: () => showDir(data.parent) }, '上一层') : null,
          h('div.grow.mono.small.truncate', { title: data.path }, data.path)),
        h('div.mt-3.panel', { style: { maxHeight: '46vh', overflow: 'auto' } },
          data.dirs.length || data.files.length
            ? h('div.list',
                ...data.dirs.map((d) =>
                  h('button.list-row', { onclick: () => showDir(d.path) },
                    h('span.muted', '▸'), h('div.grow.name', d.name))),
                ...data.files.slice(0, 400).map((f) =>
                  h('div.list-row', { style: { cursor: 'default' } },
                    h('div.grow', h('div.name.muted', f.name)),
                    h('span.xsmall.dim', fmtBytes(f.size)))))
            : empty('这个目录是空的')),
        // 导入模式。真实仪器数据是「一个子文件夹一次测量」，这条放前面。
        //
        // 为什么不能靠命名规则自动搞定：默认规则里的 {sample} 会把每个
        // Data.csv 都命名成「Data」，所有测量挤成同一个样品。所以必须
        // 在这里明确选一次。
        h('div.mt-3.field',
          h('label.field-label', '这个目录怎么导'),
          h('div.col.gap-2',
            h('label.check',
              h('input#modeFolders', { type: 'radio', name: 'scanMode', checked: true }),
              h('div.min0',
                h('span.small', '按子文件夹 —— 每个子文件夹算一次测量'),
                h('div.xsmall.dim',
                  '认 ', h('span.mono', 'ZG0013_2026…/Data.csv'), ' 这种结构。',
                  '样品名取完整文件夹名，样品号和测量时间从名字里拆出来。'))),
            h('label.check',
              h('input#modeFiles', { type: 'radio', name: 'scanMode' }),
              h('div.min0',
                h('span.small', '按文件名 —— 每个文件算一个样品'),
                h('div.xsmall.dim', '走「设置 → 命名规则」里那几条规则。'))))),

        h('div.row-between.mt-3',
          h('label.check',
            h('input#recursive', { type: 'checkbox', checked: true }),
            h('span.small', '包含所有子目录（只对「按文件名」有效）')),
          h('button.btn.btn-primary', {
            onclick: (e) => scanAndPreview(
              cursor,
              body.querySelector('#modeFolders').checked ? 'folders' : 'files',
              body.querySelector('#recursive').checked,
              e.target),
          }, '扫描这个目录')));
    } catch (err) {
      mount(body, errorBox(err, () => showDir(path)));
    }
  }

  async function scanAndPreview(path, mode, recursive, button) {
    busy(button, true);
    let result;
    try {
      // api.scan 的第二个参数是**选项对象**。以前这里直接传了个布尔，
      // 展开之后什么都没剩下 —— 那个「包含所有子目录」的勾一直是摆设。
      result = await api.scan(path, { mode, recursive });
    } catch (err) {
      busy(button, false);
      mount(body, errorBox(err, showRoots));
      return;
    }
    busy(button, false);
    rememberPath(path);        // 扫得动才记 —— 记一个打不开的路径是帮倒忙
    renderPreview(result);
  }

  function renderPreview(result) {
    // 跳过的子文件夹要说出来。静默少几个的话，你只会在很久以后
    // 发现「怎么少了一次测量」，那时候已经想不起来是哪一步丢的。
    const skipped = result.skipped || [];
    const skipNote = skipped.length
      ? h('div.notice.notice-warn.mt-3',
          h('div.grow',
            h('div.small.strong', `跳过了 ${skipped.length} 个子文件夹`),
            h('div.xsmall.dim.mt-2',
              skipped.slice(0, 8).map((s) => `${s.folder}（${s.reason}）`).join('　·　')
              + (skipped.length > 8 ? `　…还有 ${skipped.length - 8} 个` : ''))))
      : null;

    if (!result.count) {
      mount(body, empty(
        result.mode === 'folders'
          ? '这个目录下没有含 Data.csv 的子文件夹'
          : '这个目录里没有可导入的文件',
        h('button.btn', { onclick: showRoots }, '换一个目录')),
        skipNote);
      return;
    }

    const rows = result.files;
    const unmatched = rows.filter((r) => !r.matched).length;

    const table = h('table.data',
      h('thead', h('tr',
        h('th', '文件'), h('th', '落地方式'), h('th', '样品'), h('th', '批次'),
        h('th', '方法'), h('th.num', '大小'))),
      h('tbody', ...rows.slice(0, 500).map((r, i) => h('tr',
        h('td', h('div.truncate', { style: { maxWidth: '300px' }, title: r.display_path },
                  r.display_path)),
        h('td', h('span.status.' + (r.storage_mode === 'copied' ? 'status-accent' : 'status-idle'),
                  r.storage_mode === 'copied' ? '复制' : '原地引用')),
        h('td', h('input.input', {
          value: r.sample || '', placeholder: '未解析',
          style: { width: '110px', padding: '3px 6px' },
          oninput: (e) => { rows[i].sample = e.target.value.trim(); },
        })),
        h('td.small', r.batch || '—'),
        h('td.small', r.method || '—'),
        h('td.num.small', fmtBytes(r.size))))));

    mount(body,
      h('div.row-between',
        h('div',
          h('div.strong', result.mode === 'folders'
            ? `扫描到 ${result.count} 个子文件夹，每个算一个样品`
            : `扫描到 ${result.count} 个文件`),
          h('div.small.muted',
            `${result.to_copy} 个会复制进工作区 · ${result.to_reference} 个保持原位引用` +
            (unmatched ? ` · ${unmatched} 个没解析出样品名` : ''))),
        h('button.btn.btn-sm', { onclick: showRoots }, '换一个目录')),
      skipNote,
      unmatched
        ? h('div.notice.mt-3',
            h('div.grow',
              `有 ${unmatched} 个文件没能从文件名解析出样品名。可以在下表直接填，`,
              '或到「设置 → 命名规则」改规则后重新扫描。'))
        : null,
      h('div.mt-3.panel', { style: { maxHeight: '44vh', overflow: 'auto' } }, table),
      rows.length > 500 ? h('p.xsmall.dim.mt-2', `表格只显示前 500 行，导入会处理全部 ${rows.length} 个`) : null,
      h('div.row.gap-2.mt-4',
        h('button.btn.btn-primary', {
          onclick: async (e) => {
            busy(e.target, true);
            try {
              const rep = await api.importFiles(rows, result.root);
              const c = rep.counts;
              toast(`导入 ${c.imported} 个文件` +
                    (c.duplicates ? `，跳过 ${c.duplicates} 个重复` : '') +
                    (c.failed ? `，${c.failed} 个失败` : ''),
                    c.failed ? 'err' : 'ok');
              m.close();
              onDone?.(rep);
            } catch (err) {
              busy(e.target, false);
              toast(err.message, 'err', 6000);
            }
          },
        }, `导入这 ${result.count} 个文件`),
        h('button.btn', { onclick: () => m.close() }, '取消')));
  }

  showRoots();
  return m;
}

/** 拖拽上传：便捷但会复制所有文件（浏览器不给真实路径）。 */
export function bindDropUpload(node, onDone) {
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  let depth = 0;
  node.addEventListener('dragenter', (e) => { stop(e); depth++; node.classList.add('is-dragover'); });
  node.addEventListener('dragover', stop);
  node.addEventListener('dragleave', (e) => {
    stop(e); if (--depth <= 0) { depth = 0; node.classList.remove('is-dragover'); }
  });
  node.addEventListener('drop', async (e) => {
    stop(e); depth = 0; node.classList.remove('is-dragover');
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    const t = toast(`正在上传 ${files.length} 个文件…`, 'info', 60000);
    try {
      const rep = await api.upload(files);
      t.remove();
      toast(`导入 ${rep.counts.imported} 个文件（拖拽通道一律复制）`, 'ok');
      onDone?.(rep);
    } catch (err) {
      t.remove();
      toast(err.message, 'err', 6000);
    }
  });
}
