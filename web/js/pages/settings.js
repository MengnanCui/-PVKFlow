// 设置：导入策略、命名规则（带实时预览）、模型配置。

import { api } from '../api.js';
import { infoDot } from '../components/info.js';
import { h, mount, clear, render, toast, busy, errorBox, empty, skeletonRows,
         confirmDialog,
         fmtBytes, fmtInt } from '../ui.js';

export const meta = {
  id: 'settings',
  title: '设置',
  desc: '导入策略、命名规则、模型接入',
};

export function view(host) {
  return render(host,
    () => Promise.all([api.settings(), api.models(), api.skills(), api.modules()]),
    ([cfg, models, skills, mods]) => h('div',
      importSection(cfg.settings),
      namingSection(cfg.settings),
      moduleSection(mods),
      modelSection(models),
      skillSection(skills),
      workspaceSection(cfg)));
}

// ------------------------------------------------------------------ 导入策略
function importSection(s) {
  const copyBox = h('textarea.textarea.mono', {
    value: (s.copy_extensions || []).join(' '),
    style: { minHeight: '60px' },
  });
  const refBox = h('textarea.textarea.mono', {
    value: (s.reference_extensions || []).join(' '),
    style: { minHeight: '60px' },
  });
  const unknown = h('select.select',
    ...[['reference', '只登记路径（推荐）'], ['copy', '复制进工作区']].map(([v, t]) =>
      h('option', { value: v, selected: s.unknown_policy === v }, t)));

  return section(h('span', '导入策略', infoDot('storage_mode')),
                 '决定一个文件是被复制进工作区，还是只登记原始路径',
    h('div.panel.panel-body',
      h('p.small.muted.measure',
        '文本类文件小、易丢、经常被人手改，复制一份才谈得上可复现；',
        '图像动辄几百 MB，搬一遍不划算，所以只登记路径并生成缩略图。',
        '代价是原图被移动或改名后会断链 —— 界面上会标成「丢失」，不会静默失败。'),
      h('div.mt-4.col.gap-4',
        h('div.field',
          h('label.field-label', '复制进工作区的扩展名'),
          copyBox,
          h('div.field-help', '空格分隔。这些文件按 sha256 内容寻址存放，重复导入自动去重。',
                                infoDot('dedup'))),
        h('div.field',
          h('label.field-label', '只登记路径的扩展名'),
          refBox,
          h('div.field-help', '空格分隔。原文件保持不动，另外生成缩略图供界面预览。')),
        h('div.field',
          h('label.field-label', '两个名单都没有的扩展名'),
          unknown)),
      h('div.mt-4',
        h('button.btn.btn-primary', {
          onclick: async (e) => {
            busy(e.target, true);
            try {
              await api.saveSettings({
                copy_extensions: parseExts(copyBox.value),
                reference_extensions: parseExts(refBox.value),
                unknown_policy: unknown.value,
              });
              toast('导入策略已保存', 'ok');
            } catch (err) { toast(err.message, 'err'); }
            busy(e.target, false);
          },
        }, '保存'))));
}

const parseExts = (t) => t.split(/[\s,;]+/).map((x) => x.trim().toLowerCase())
  .filter(Boolean).map((x) => (x.startsWith('.') ? x : '.' + x));

// ------------------------------------------------------------------ 命名规则
function namingSection(s) {
  const rulesBox = h('textarea.textarea.mono', {
    value: (s.naming_rules || []).join('\n'),
    style: { minHeight: '92px' },
  });
  const sampleBox = h('textarea.textarea.mono', {
    value: 'B12/B12_S1_jv.csv\nB12_S3_thickness.dat\n2026-08-20_A7.txt',
    style: { minHeight: '68px' },
  });
  const out = h('div.mt-3');

  const tryRules = async () => {
    const paths = sampleBox.value.split('\n').map((x) => x.trim()).filter(Boolean);
    const rules = rulesBox.value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!paths.length) { mount(out, empty('上面填几个文件名试试')); return; }
    mount(out, skeletonRows(2));
    try {
      const r = await api.namingPreview(paths, rules);
      mount(out, h('div.panel.panel-body.flush',
        h('table.data',
          h('thead', h('tr', h('th', '文件'), h('th', '样品'), h('th', '批次'),
                       h('th', '方法'), h('th', '命中规则'))),
          h('tbody', ...r.rows.map((row) => h('tr',
            h('td.mono.xsmall', row.path),
            h('td', row.sample ? h('span.strong', row.sample) : h('span.status.status-warn', '没解析出')),
            h('td.small.muted', row.batch || '—'),
            h('td.small.muted', row.method || '—'),
            h('td.mono.xsmall.dim', row.rule || '—')))))));
    } catch (err) { mount(out, errorBox(err)); }
  };

  return section(h('span', '命名规则', infoDot('sample_identity')),
                 '从文件名或路径自动解析样品身份，导入时先给预览，不对可以手改',
    h('div.panel.panel-body',
      h('div.col.gap-4',
        h('div.field',
          h('label.field-label', '规则（每行一条，按顺序匹配，第一条命中为准）'),
          rulesBox,
          h('div.field-help',
            h('span.mono', '{sample}'), ' 必需；', h('span.mono', '{batch}'), '、',
            h('span.mono', '{method}'), ' 可选；', h('span.mono', '{*}'), ' 表示这一段丢弃。',
            '另有两条内置规则：', h('span.mono', '@parent'), '（父文件夹名即样品）、',
            h('span.mono', '@parent2'), '（祖父文件夹名即样品，父文件夹当方法）。')),
        h('div.field',
          h('label.field-label', '拿几个真实文件名试一下'),
          sampleBox)),
      h('div.row.gap-2.mt-3',
        h('button.btn', { onclick: tryRules }, '预览解析结果'),
        h('button.btn.btn-primary', {
          onclick: async (e) => {
            busy(e.target, true);
            try {
              await api.saveSettings({
                naming_rules: rulesBox.value.split('\n').map((x) => x.trim()).filter(Boolean),
              });
              toast('命名规则已保存', 'ok');
            } catch (err) { toast(err.message, 'err'); }
            busy(e.target, false);
          },
        }, '保存')),
      out));
}

// ------------------------------------------------------------------ 功能模块
//
// 同事加功能走的是这里：一个模块 = 一个文件夹 = 一个 zip。
// 收发靠导入导出，不靠 Git —— 网络受限、不会用分支，都不影响。
function moduleSection(mods) {
  const list = mods.modules || [];
  const errs = mods.errors || [];
  const status = h('div.mt-3');

  const reload = async (e) => {
    busy(e.target, true);
    try {
      const r = await api.reloadModules();
      toast(`重载完成：${r.count} 个模块`
        + (r.errors.length ? `，${r.errors.length} 个加载失败` : ''),
        r.errors.length ? 'warn' : 'ok');
      setTimeout(() => window.dispatchEvent(
        new CustomEvent('hte:nav', { detail: 'settings' })), 400);
    } catch (err) { toast(err.message, 'err'); }
    busy(e.target, false);
  };

  const fileInput = h('input', {
    type: 'file', accept: '.zip', style: { display: 'none' },
    onchange: async (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      mount(status, h('div.small.muted', '正在验证…'));
      try {
        const r = await api.importModule(f);
        if (r.installed) {
          mount(status, h('div.notice.notice-ok', h('div.grow',
            h('div.small', `装好了：${r.report.module_id}`))));
          setTimeout(() => window.dispatchEvent(
            new CustomEvent('hte:nav', { detail: 'settings' })), 700);
        } else {
          // 装不上要把每一条都摊开 —— 同事（和他的模型）就靠这段改
          mount(status, reportBox(r.report, r.hint));
        }
      } catch (err) { mount(status, errorBox(err)); }
      e.target.value = '';
    },
  });

  return section(h('span', '功能模块', infoDot('module_contract')),
    '同事写的功能。放一个文件夹进工作区，或者直接导入 zip —— 不用 Git、不用分支',
    h('div.panel.panel-body',
      h('div.notice',
        h('div.grow',
          h('div.small.strong', '模块只交算法和声明，界面由平台画'),
          h('p.xsmall.dim.mt-2',
            '所以同事加的功能和平台其它部分长得一模一样 —— 他碰不到 CSS。',
            '写法见 ', h('span.mono', 'docs/MODULE_AUTHORING.md'),
            '，模板在 ', h('span.mono', `${mods.modules_dir}/_template/`), '。'))),

      errs.length
        ? h('div.mt-4',
            h('div.small.strong.danger', `${errs.length} 个模块加载失败`),
            ...errs.map((e) => h('div.notice.notice-danger.mt-2',
              h('div.grow',
                h('div.small.mono', e.source),
                h('div.xsmall.mt-1', e.error),
                e.detail ? h('details.mt-2',
                  h('summary.xsmall.dim', { style: { cursor: 'pointer' } }, '详细'),
                  h('pre.xsmall', e.detail)) : null))))
        : null,

      list.length
        ? h('div.panel.panel-body.flush.mt-4', moduleTable(list, status))
        : h('p.small.muted.mt-4', '还没有装任何模块。'),

      h('div.row.gap-2.mt-3',
        h('button.btn', { onclick: reload }, '重载模块'),
        h('button.btn', { onclick: () => fileInput.click() }, '导入 zip…'),
        fileInput,
        h('span.xsmall.dim.grow',
          `算子：${(mods.ops || []).map((o) => o.name).join('、') || '无'}`)),
      status));
}

function moduleTable(list, status) {
  return h('table.table',
    h('thead', h('tr',
      h('th', '名字'), h('th', 'id'), h('th', '版本'), h('th', '面板'),
      h('th', '批处理曲线'), h('th', '来源'), h('th', ''))),
    h('tbody', ...list.map((m) => h('tr',
      h('td.strong', m.name),
      h('td.mono.xsmall.dim', m.id),
      h('td.mono.xsmall', m.version),
      h('td.xsmall',
        `${m.panels.length} 格`,
        m.live_panels.length
          ? h('span.xsmall.dim', `　${m.live_panels.length} 格可实时拖动`)
          : null),
      h('td.xsmall.mono.dim',
        (m.batch_curves || []).map((c) => c.name).join(', ') || '—'),
      h('td', h('span.tag', m.origin === 'builtin' ? '平台自带' : '同事装的')),
      h('td.row.gap-1',
        h('button.btn.btn-ghost.btn-xs', {
          onclick: async (e) => {
            busy(e.target, true);
            try {
              const r = await api.validateModule(m.id);
              mount(status, reportBox(r));
            } catch (err) { mount(status, errorBox(err)); }
            busy(e.target, false);
          },
        }, '验证'),
        h('a.btn.btn-ghost.btn-xs', { href: api.moduleExportUrl(m.id) }, '导出'),
        m.origin === 'builtin' ? null : h('button.btn.btn-ghost.btn-xs', {
          onclick: async () => {
            if (!await confirmDialog(`卸载「${m.name}」？文件会被删掉。`,
                                     { danger: true, confirmLabel: '卸载' })) return;
            try {
              await api.uninstallModule(m.id);
              toast('已卸载', 'ok');
              setTimeout(() => window.dispatchEvent(
                new CustomEvent('hte:nav', { detail: 'settings' })), 400);
            } catch (err) { toast(err.message, 'err'); }
          },
        }, '卸载'))))));
}

/** 验证报告。**每一条都要看得见** —— 这段就是同事拿去喂给模型的东西。 */
function reportBox(r, hint) {
  if (r.ok) {
    return h('div.notice.notice-ok', h('div.grow',
      h('div.small', `${r.module_id} 验证通过`),
      h('div.xsmall.dim.mt-1', `查了：${(r.checked || []).join(' · ')}`),
      ...(r.warnings || []).map((w) => h('div.xsmall.warn-text.mt-1', '⚠ ' + w))));
  }
  return h('div.notice.notice-danger', h('div.grow',
    h('div.small.strong', `没通过，${r.errors.length} 条要改`),
    ...(r.errors || []).map((e) => h('pre.xsmall.mt-2', { style: {
      whiteSpace: 'pre-wrap', margin: '4px 0' } }, '✗ ' + e)),
    ...(r.warnings || []).map((w) => h('div.xsmall.warn-text.mt-1', '⚠ ' + w)),
    hint ? h('div.xsmall.dim.mt-2', hint) : null));
}

// ------------------------------------------------------------------ 模型
//
// 两条路：**表单**给日常用（三个框填完就能用），**粘 JSON** 给需要多个
// provider、多个模型的时候。表单在上面，因为九成情况下够了 ——
// 让人为了填一个地址先去读一段 JSON 结构，是把内部实现当成了使用说明。
function modelSection(models) {
  const providers = models.providers || [];
  const status = h('div.mt-3');

  return section('模型', 'OpenAI 兼容协议。没配也没关系 —— 识别、推荐、数据检查全部走规则引擎',
    h('div.panel.panel-body',
      h('div.notice',
        h('div.grow',
          h('div.small.strong', '密钥存在哪'),
          h('p.xsmall.dim.mt-2',
            '保存后写入 ', h('span.mono', 'workspace/config/providers.json'),
            '。这个目录在 .gitignore 里，密钥不会被提交进仓库，也不会随任何',
            '接口原样返回（界面上只看得到打码后的前后几位）。'))),

      providers.length
        ? h('div.mt-4',
            h('div.small.strong', '已配置'),
            h('div.panel.panel-body.flush.mt-2', providerTable(providers, status)))
        : h('p.small.muted.mt-4', '还没有配置任何 provider。填下面三个框就能用。'),

      status,
      simpleForm(providers, models.presets),
      advancedJson(providers)));
}

/**
 * 填地址 → 填密钥 → **点一下拉出可用模型** → 勾几个 → 测一下 → 保存。
 *
 * 原来模型名要手打：网关文档里那个 id 区分大小写，打错了要到发第一条消息
 * 才报错，而且报的是网关的原话。现在按 OpenAI 兼容协议的 /models 拉一份回来选。
 *
 * 拉不到是**常见情况**（不少网关没实现 /models），所以手填那条路留着，
 * 不是把它藏起来。
 */
function simpleForm(providers, presets) {
  const first = providers[0] || {};
  const saved = (first.models || []).map((m) => m.id);

  // 本地状态：拉回来的候选、勾中的、以及手填的那些
  const F = { found: [], chosen: new Set(saved), manual: saved.join(', ') };

  const url = h('input.input', {
    type: 'text', placeholder: 'https://你的网关/v1',
    // 填过一次就一直回填 —— 你的网关地址存在本机配置里，不在仓库里
    value: first.base_url || '', style: { width: '100%' } });
  const key = h('input.input', {
    // type=password 免得在会议室投屏时把密钥直接投出去
    type: 'password', placeholder: 'sk-…',
    autocomplete: 'off', style: { width: '100%' } });
  const name = h('input.input', {
    type: 'text', placeholder: '默认叫「我的模型」',
    value: providers.length ? first.name : '', style: { width: '100%' } });
  const manual = h('input.input', {
    type: 'text', placeholder: 'Qwen3.6-27B', value: F.manual,
    style: { width: '100%' },
    oninput: (e) => { F.manual = e.target.value; } });

  const modelBox = h('div.model-pick');
  const field = (label, node, help) => h('div.field',
    h('label.field-label', label), node,
    help ? h('div.field-help', help) : null);

  // 预设只是**填进地址栏**，不锁死 —— 填完照样能改
  const presetPick = h('select.select', {
    onchange: (e) => {
      if (!e.target.value) return;
      url.value = e.target.value;
      e.target.value = '';
      drawModels();
    },
  },
    h('option', { value: '' }, '常见地址…'),
    ...(presets || []).map((p) => h('option', { value: p.base_url },
      `${p.name} — ${p.base_url}`)));

  function drawModels() {
    if (!F.found.length) {
      mount(modelBox,
        h('div.xsmall.dim',
          '还没拉过列表。填好上面两项后点「获取可用模型」，'
          + '或者直接在下面手填模型名。'));
      return;
    }
    mount(modelBox,
      h('div.xsmall.dim.mb-2', `拉到 ${F.found.length} 个，勾选要用的：`),
      h('div.model-grid', ...F.found.map((m) => {
        const cb = h('input', {
          type: 'checkbox', checked: F.chosen.has(m.id),
          onchange: () => {
            if (cb.checked) F.chosen.add(m.id); else F.chosen.delete(m.id);
            manual.value = F.manual = [...F.chosen].join(', ');
          },
        });
        return h('label.model-opt', cb, h('span.mono.xsmall', m.id),
          m.owned_by ? h('span.xsmall.dim', m.owned_by) : null);
      })));
  }

  const status = h('div.mt-3');
  const chosenIds = () => {
    const fromBox = [...F.chosen];
    const typed = F.manual.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean);
    return [...new Set([...fromBox, ...typed])];
  };

  const discoverBtn = h('button.btn', {
    onclick: async (e) => {
      const u = url.value.trim();
      if (!u) { toast('先填接口地址', 'err'); return; }
      busy(e.target, true);
      clear(status);
      try {
        const r = await api.discoverModels({ base_url: u, api_key: key.value.trim() });
        F.found = r.models || [];
        drawModels();
        mount(status, h('div.notice.notice-ok',
          h('div.grow', h('div.small',
            `拉到 ${r.count} 个模型` + (r.used_saved_key ? '（用的是已经存着的密钥）' : '')))));
      } catch (err) {
        F.found = [];
        drawModels();
        // 拉不到不是死路。把「手填一样能用」摆在错误旁边。
        mount(status, h('div.notice.notice-warn',
          h('div.grow',
            h('div.small', err.message),
            h('p.xsmall.dim.mt-2',
              '不少网关没有提供模型列表接口。下面手填模型名一样能用。'))));
      }
      busy(e.target, false);
    },
  }, '获取可用模型');

  const testBtn = h('button.btn', {
    onclick: async (e) => {
      busy(e.target, true);
      try {
        const r = await api.testModel(name.value.trim() || first.name || undefined,
                                      chosenIds()[0]);
        mount(status, r.ok
          ? h('div.notice.notice-ok', h('div.grow',
              h('div.small', `连通 · ${r.model} · ${r.latency_ms} ms`),
              h('div.xsmall.dim.mt-1', `它回了：${r.reply}`)))
          : h('div.notice.notice-danger', h('div.grow',
              h('div.small', r.error))));
      } catch (err) { toast(err.message, 'err', 6000); }
      busy(e.target, false);
    },
  }, '连接测试');

  drawModels();

  return h('div.mt-4',
    h('div.small.strong.mb-2', providers.length ? '改一个 / 加一个' : '填完就能用'),
    h('div.col.gap-3',
      field('接口地址（baseUrl）',
        h('div.row.gap-2', h('div.grow', url), presetPick),
        ['结尾要带 ', h('span.mono', '/v1'), '。',
         '你自己的网关地址填过一次就会一直留在这儿 —— 它存在本机的 ',
         h('span.mono', 'workspace/config/providers.json'), '，不会进仓库。']),
      field('密钥（apiKey）', key,
        providers.length && first.has_key
          ? '留空 = 不改，继续用已经存着的那个密钥（拉模型列表也会用它）'
          : '直接粘贴，不用加引号'),
      h('div.field',
        h('label.field-label', '模型'),
        h('div.row.gap-2.mb-2', discoverBtn, testBtn),
        modelBox,
        status,
        h('div.mt-3', manual),
        h('div.field-help',
          '上面勾的会同步到这一栏。也可以直接手打，多个用逗号分开 —— ',
          '存下来的模型在右侧 AI 助手里可以随时切换。')),
      field('给它起个名字（可选）', name, '只是界面上显示用，随便叫')),
    h('div.row.gap-2.mt-3',
      h('button.btn.btn-primary', {
        onclick: async (e) => {
          const u = url.value.trim();
          const ids = chosenIds();
          if (!u) { toast('接口地址要填', 'err'); return; }
          if (!ids.length) { toast('至少选或填一个模型', 'err'); return; }
          const k = key.value.trim();
          if (!k && !(providers.length && first.has_key)) {
            toast('还没有存过密钥，这次要填', 'err'); return;
          }
          busy(e.target, true);
          try {
            await api.saveSimpleModel({
              name: name.value.trim() || first.name || '我的模型',
              base_url: u, api_key: k, model_ids: ids,
            });
            toast(`已保存 ${ids.length} 个模型。右上角「AI 助手」就能用了`, 'ok');
            setTimeout(() => window.dispatchEvent(
              new CustomEvent('hte:nav', { detail: 'settings' })), 500);
          } catch (err) { toast(err.message, 'err', 6000); }
          busy(e.target, false);
        },
      }, '保存并启用')));
}

/** 需要多个 provider / 多个模型时走这条。默认折叠，别让它挡住上面那三个框。 */
function advancedJson(providers) {
  const editor = h('textarea.textarea.mono', {
    placeholder: '把 providers 配置粘贴到这里',
    style: { minHeight: '200px', fontSize: '12px' },
  });

  return h('details.mt-5',
    h('summary.small.muted', { style: { cursor: 'pointer' } },
      '高级：直接粘 JSON（要配多个 provider 或多个模型时用）'),
    h('div.mt-3.field',
      editor,
      h('div.field-help',
        '结构：', h('span.mono', '{ "providers": { "名字": { "baseUrl", "apiKey", "models": [...] } } }'),
        '　⚠️ 保存会**整体替换**现有配置，不是追加。'),
      h('div.row.gap-2.mt-3',
        h('button.btn.btn-primary', {
          onclick: async (e) => {
            if (!editor.value.trim()) { toast('先粘贴配置内容', 'err'); return; }
            busy(e.target, true);
            try {
              await api.saveModels(editor.value);
              toast('模型配置已保存', 'ok');
              setTimeout(() => window.dispatchEvent(
                new CustomEvent('hte:nav', { detail: 'settings' })), 500);
            } catch (err) { toast(err.message, 'err', 6000); }
            busy(e.target, false);
          },
        }, '保存配置'),
        h('button.btn', {
          onclick: async () => {
            const { example } = await api.modelExample();
            editor.value = example;
            toast('已填入模板，把 baseUrl 和 apiKey 换成你的', 'info');
          },
        }, '填入模板'))));
}

function providerTable(providers, status) {
  const rows = [];
  for (const p of providers) {
    const models = p.models.length ? p.models : [{ id: '', name: '（没有配模型）' }];
    models.forEach((m, i) => {
      rows.push(h('tr',
        h('td', i === 0 ? h('span.strong', p.name) : null),
        h('td.mono.xsmall.truncate', { title: p.base_url }, i === 0 ? p.base_url : ''),
        h('td.mono.xsmall.dim', i === 0 ? (p.api_key_masked || '未设置') : ''),
        h('td',
          m.id ? h('span.mono.small', m.id) : h('span.small.dim', m.name),
          m.vision ? h('span.tag', { style: { marginLeft: '6px' } }, '视觉') : null),
        h('td', m.id ? testButton(p.name, m.id, status) : null)));
    });
  }
  return h('table.data',
    h('thead', h('tr', h('th', 'Provider'), h('th', '地址'), h('th', '密钥'),
                 h('th', '模型'), h('th', ''))),
    h('tbody', ...rows));
}

function testButton(providerName, modelId, status) {
  return h('button.btn.btn-sm', {
    onclick: async (e) => {
      busy(e.target, true);
      try {
        const r = await api.testModel(providerName, modelId);
        if (r.ok) {
          await api.saveSettings({ active_provider: providerName, active_model: modelId });
          mount(status, h('div.notice.notice-accent',
            h('div.grow', `${r.provider} / ${r.model} 可用 · ${r.latency_ms} ms` +
              (r.reply ? ` · 回复「${r.reply}」` : '') + ' · 已设为当前模型')));
        } else {
          mount(status, h('div.notice.notice-danger', h('div.grow', r.error)));
        }
      } catch (err) {
        mount(status, h('div.notice.notice-danger', h('div.grow', err.message)));
      }
      busy(e.target, false);
    },
  }, '测试并启用');
}

// ------------------------------------------------------------------ Skill
function skillSection(data) {
  return section('Skill', `已加载 ${data.skills.length} 个处理能力`,
    h('div.panel.panel-body',
      h('p.small.muted.measure',
        '把一个目录（里面放 ', h('span.mono', 'skill.py'), ' 或 ',
        h('span.mono', 'SKILL.md'), '）丢进 ', h('span.mono', 'workspace/skills/'),
        '，然后在数据处理页点「重载 Skill」就生效，不用重启。写法见 ',
        h('span.mono', 'docs/SKILL_CONTRACT.md'), '。'),

      data.errors?.length
        ? h('div.notice.notice-danger.mt-3',
            h('div.grow',
              h('div.small.strong', `${data.errors.length} 个 skill 加载失败`),
              ...data.errors.map((e) => h('div.mt-2',
                h('div.mono.xsmall', e.source),
                h('div.xsmall.dim', e.error)))))
        : null,

      h('div.panel.panel-body.flush.mt-3', skillTable(data.skills))));
}

/** 已加载 skill 的清单。同样拆出来保持可读。 */
function skillTable(skills) {
  const originText = { builtin: '自带', user: '你添加的', 'skill.md': 'SKILL.md' };
  return h('table.data',
    h('thead', h('tr', h('th', '名称'), h('th', 'ID'), h('th', '类别'),
                 h('th', '版本'), h('th', '来源'), h('th', '状态'))),
    h('tbody', ...skills.map((s) => h('tr',
      h('td.strong', s.name),
      h('td.mono.xsmall.muted', s.id),
      h('td.small.muted', s.category),
      h('td.mono.xsmall.muted', s.version),
      h('td.small.muted', originText[s.origin] || s.origin),
      h('td', s.ready
        ? h('span.status.status-ok.small', '可运行')
        : h('span.status.status-warn.small', { title: s.ready_note }, '待接入'))))));
}

function workspaceSection(cfg) {
  const cacheHost = h('div.mt-4');

  // 缓存占用异步取：它要遍历目录，不该拖慢整个设置页
  (async () => {
    let c;
    try { c = await api.cacheStatus(); } catch { return; }
    const used = c.bytes || 0;
    const limit = c.limit_bytes || 0;
    mount(cacheHost,
      h('div.row-between',
        h('div',
          h('div.small.strong', '解析缓存'),
          h('div.xsmall.dim.mt-1',
            `${fmtInt(c.files || 0)} 个文件 · ${fmtBytes(used)}`,
            limit ? ` / 上限 ${fmtBytes(limit)}` : '',
            '　—— 删了只是下次解析慢一点，原始数据不受影响')),
        h('button.btn.btn-sm', {
          disabled: !c.files,
          onclick: async (e) => {
            busy(e.target, true);
            try {
              const r = await api.clearCache();
              toast(`已清空，腾出 ${fmtBytes(r.freed_bytes ?? used)}`, 'ok');
              window.dispatchEvent(new CustomEvent('hte:nav', { detail: 'settings' }));
            } catch (err) { toast(err.message, 'err'); }
            busy(e.target, false);
          },
        }, '清空缓存')),
      limit ? h('div.bar.mt-2',
        h('i', { style: { width: `${Math.min(100, (used / limit) * 100)}%` } })) : null);
  })();

  return section('工作区', '所有持久化状态都在这一个目录里，整个拷走就能换机器',
    h('div.panel.panel-body',
      h('div.col.gap-3',
        kv('工作区目录', cfg.workspace),
        kv('模型配置文件', cfg.providers_path),
        kv('版本', cfg.version)),
      h('p.xsmall.dim.mt-3',
        '备份就是把这个目录整个复制走 —— 没有别的地方藏东西。',
        '不可再生的是 ', h('span.mono', 'hte.db'), ' 和 ', h('span.mono', 'raw/'), '；',
        h('span.mono', 'cache/'), '、', h('span.mono', 'derived/'), '、',
        h('span.mono', 'tables/'), ' 都能重算。'),
      cacheHost));
}

const kv = (k, v) => h('div.row.gap-3',
  h('span.small.muted', { style: { width: '110px', flex: 'none' } }, k),
  h('span.mono.small.truncate', { title: v }, v));

function section(title, note, body) {
  return h('div.section',
    h('div.section-head',
      h('div.section-title', title),
      note ? h('div.section-note', note) : null),
    body);
}
