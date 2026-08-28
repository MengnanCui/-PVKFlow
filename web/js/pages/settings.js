// 设置：导入策略、命名规则（带实时预览）、模型配置。

import { api } from '../api.js';
import { infoDot } from '../components/info.js';
import { h, mount, render, toast, busy, errorBox, empty, skeletonRows,
         fmtBytes, fmtInt } from '../ui.js';

export const meta = {
  id: 'settings',
  title: '设置',
  desc: '导入策略、命名规则、模型接入',
};

export function view(host) {
  return render(host, () => Promise.all([api.settings(), api.models(), api.skills()]),
    ([cfg, models, skills]) => h('div',
      importSection(cfg.settings),
      namingSection(cfg.settings),
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
      simpleForm(providers),
      advancedJson(providers)));
}

/** 三个框：接口地址、密钥、模型名。填完就能用。 */
function simpleForm(providers) {
  const first = providers[0] || {};
  const firstModel = (first.models || [])[0] || {};

  const url = h('input.input', {
    type: 'text', placeholder: 'https://你的网关/v1',
    value: first.base_url || '', style: { width: '100%' } });
  const key = h('input.input', {
    // type=password 免得在会议室投屏时把密钥直接投出去
    type: 'password', placeholder: 'sk-…',
    autocomplete: 'off', style: { width: '100%' } });
  const model = h('input.input', {
    type: 'text', placeholder: 'Qwen3.6-27B',
    value: firstModel.id || '', style: { width: '100%' } });
  const name = h('input.input', {
    type: 'text', placeholder: '默认叫「我的模型」',
    value: providers.length ? first.name : '', style: { width: '100%' } });

  const field = (label, node, help) => h('div.field',
    h('label.field-label', label), node,
    help ? h('div.field-help', help) : null);

  return h('div.mt-4',
    h('div.small.strong.mb-2', providers.length ? '改一个 / 加一个' : '填这三个就能用'),
    h('div.col.gap-3',
      field('接口地址（baseUrl）', url,
        ['结尾要带 ', h('span.mono', '/v1'), '。OpenAI 兼容网关、vLLM、本地模型都行。']),
      field('密钥（apiKey）', key,
        providers.length && first.has_key
          ? '留空 = 不改，继续用已经存着的那个密钥'
          : '直接粘贴，不用加引号'),
      field('模型名（model id）', model, '网关文档里那个 id，区分大小写'),
      field('给它起个名字（可选）', name, '只是界面上显示用，随便叫')),
    h('div.row.gap-2.mt-3',
      h('button.btn.btn-primary', {
        onclick: async (e) => {
          const u = url.value.trim(), m = model.value.trim();
          if (!u || !m) { toast('接口地址和模型名都要填', 'err'); return; }
          const k = key.value.trim();
          if (!k && !(providers.length && first.has_key)) {
            toast('还没有存过密钥，这次要填', 'err'); return;
          }
          busy(e.target, true);
          try {
            await api.saveSimpleModel({
              name: name.value.trim() || first.name || '我的模型',
              base_url: u, api_key: k, model_id: m,
            });
            toast('已保存。右上角「AI 助手」就能用了', 'ok');
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
