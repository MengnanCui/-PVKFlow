// 关键结果的审核：可信 / 待查 / 作废。
//
// 初始状态是写结果的程序给的：平台的批处理给「可信」（它自己的检查过了），功能模块没给就是「待查」。
// 人点过之后记下时间（quality_at）—— 所以「可信」分得清是程序说的还是人看过的。
// 作废一个之后，这个 (样品, 字段) 的「当前值」退回上一次没作废的那条（key_result_current 的定义），
// 构效关系的散点、AI 看到的数、按字段筛选都跟着变。
// 作废要留理由：一年后看到「这个数被作废了」却不知道为什么，和没审核一样。

import { api } from '../api.js';
import { h, modal, toast, fmtNum, fmtTime } from '../ui.js';

export const QUALITY = { validated: '可信', review: '待查', reject: '作废' };

/** 把审核之后的当前值说成一句话。 */
export function currentNote(cur) {
  if (!cur) return '这个字段现在没有可用的值（所有结果都作废了）';
  const v = cur.value_num ?? cur.value_text;
  return `当前值：${typeof v === 'number' ? fmtNum(v) : v}${cur.unit ? ` ${cur.unit}` : ''}`
    + `（${cur.run_title || '一次处理'} · ${fmtTime(cur.run_started_at || cur.created_at)}）`;
}

/** 问一句为什么。可以不填。取消 → null。 */
export function askReason(title, hint) {
  return new Promise((resolve) => {
    const input = h('textarea.input', { rows: 2, placeholder: hint, style: { width: '100%' } });
    let done = false;
    const m = modal({
      title,
      body: h('div', h('p.small.dim', '理由会和这条结果存在一起，以后看得到。可以不填。'), input),
      foot: [
        h('button.btn', { onclick: () => { done = true; m.close(); resolve(null); } }, '取消'),
        h('button.btn.btn-primary', { onclick: () => { done = true; m.close(); resolve(input.value.trim()); } }, '确定'),
      ],
      onClose: () => { if (!done) resolve(null); },
    });
    input.focus();
  });
}

/**
 * 一组三个小按钮。r 至少要有 {id, quality}；可选 quality_note / quality_at 显示在提示里。
 * onChanged(resp) 在改完之后调（resp = {result, current}），调用方据此刷新。
 */
export function qualityControl(r, { onChanged = null, label = '' } = {}) {
  const tip = r.quality_at
    ? `${fmtTime(r.quality_at)} 审核${r.quality_note ? `，理由：${r.quality_note}` : ''}`
    : ('quality_at' in r ? '还没有人审过（现在的状态是处理程序给的）' : '');
  const pick = async (q) => {
    if (q === r.quality) return;
    let note = '';
    if (q === 'reject') {
      note = await askReason(`作废${label ? `「${label}」` : '这个结果'}`, '比如：那片样品裂了 / 这次光源没稳');
      if (note === null) return;
    }
    try {
      const resp = await api.setQuality(r.id, q, note);
      r.quality = q;
      r.quality_at = resp.result?.quality_at;
      toast(`已标为「${QUALITY[q]}」。${currentNote(resp.current)}`, q === 'reject' ? 'warn' : 'ok', 5200);
      onChanged?.(resp);
    } catch (err) { toast(err.message, 'err'); }
  };
  return h('span.quality-ctl', { role: 'group', 'aria-label': `审核${label ? `：${label}` : ''}`, title: tip },
    ...Object.entries(QUALITY).map(([q, text]) => h('button', {
      type: 'button', dataset: { q }, 'aria-pressed': String(r.quality === q),
      onclick: () => pick(q),
    }, text)));
}
