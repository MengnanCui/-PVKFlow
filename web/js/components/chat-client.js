// AI 抽屉和 ⓘ 浮层共用的三样东西：选的是哪个模型、模型下拉、流式发问。
//
// 原来两边各写一份（ⓘ 浮层里是第二个流式客户端加第二个模型选择器），
// 结果是：在 ⓘ 里换了模型，开着的抽屉不知道，要关了重开才跟上；
// 抽屉里修过的流式边界情况，ⓘ 那份没跟着修。现在只有这一份。

import { api } from '../api.js';
import { h } from '../ui.js';

/**
 * 选中的模型存 localStorage —— 这是「我这台机器上习惯用哪个」，不是平台设置，
 * 不该写进数据库让别人也跟着变。
 */
const MODEL_KEY = 'hte.ai.model';
const PICKED = 'hte:model-picked';

export function pickedModel() {
  try {
    const raw = localStorage.getItem(MODEL_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

/** 换模型。两边都听这个事件，所以在哪边换，另一边立刻跟上。 */
export function pickModel(v) {
  try {
    if (v) localStorage.setItem(MODEL_KEY, JSON.stringify(v));
    else localStorage.removeItem(MODEL_KEY);
  } catch { /* 隐私模式下写不进去，不影响这一次会话 */ }
  window.dispatchEvent(new CustomEvent(PICKED, { detail: v }));
}

export function onModelPicked(fn) {
  window.addEventListener(PICKED, (e) => fn(e.detail));
}

/** 设置里配好的全部模型：[{provider, model}]。取不到就是空的。 */
export async function configuredModels() {
  const list = [];
  try {
    const cfg = await api.models();
    for (const p of cfg.providers || []) {
      for (const m of p.models || []) list.push({ provider: p.name, model: m.id });
    }
  } catch { /* 没配或者取不到：当成一个都没有 */ }
  return list;
}

const same = (a, b) => !!a && !!b && a.model === b.model && a.provider === b.provider;

/**
 * 模型下拉。
 * 返回 {el, current}：只有一个模型时 el 为 null（没有选择余地的下拉只是噪声）；
 * current 是这次实际会用的那个（存着的那个已经不在配置里了就是 null = 用默认）。
 */
export async function modelSelect({ title }) {
  const list = await configuredModels();
  const saved = pickedModel();
  const current = list.some((x) => same(x, saved)) ? saved : null;
  if (list.length < 2) return { el: null, current };
  const sep = '\u0000';
  const el = h('select.model-switch', {
    title,
    onchange: (e) => {
      const [provider, model] = e.target.value.split(sep);
      pickModel({ provider, model });
    },
  }, ...list.map((x) => h('option', {
    value: `${x.provider}${sep}${x.model}`, selected: same(x, current),
  }, x.model)));
  return { el, current };
}

/** 别处换了模型时，让这个下拉也显示成那个。 */
export function syncSelect(el, v) {
  if (!el || !v) return;
  el.value = `${v.provider}\u0000${v.model}`;
}

/**
 * 发一条消息，边收边把回答写进 reply（{content, meta}）。
 *
 * 结束时 reply.streaming = false，并按结果填好 meta：
 * message_id / card / error / aborted（你按了停止）/ noModel（还没配模型）。
 * 不抛错 —— 调用方只管看 reply.meta 画界面。
 */
export async function streamReply(conversationId, body, { signal, reply, onDelta }) {
  try {
    await api.sendMessage(conversationId, body, {
      signal,
      onFrame: (event, data) => {
        if (event === 'meta') reply.meta.message_id = data.message_id;
        else if (event === 'delta') { reply.content += data.text; onDelta?.(); }
        else if (event === 'card') reply.meta.card = data;
        else if (event === 'error') reply.meta.error = data.message;
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') reply.meta.aborted = true;
    else if (err.kind === 'no_model' || err.status === 501) reply.meta.noModel = true;
    else reply.meta.error = err.message;
  } finally {
    reply.streaming = false;
  }
}
