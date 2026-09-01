// 按声明渲染一个功能模块。
//
// 同事的模块只交两样东西：算法（可选）和一份声明。界面从这里长出来 ——
// 面板的三行结构、控件、图注、下载菜单、ⓘ、左右等高，全部用平台自己的组件。
// **这个文件里不写任何新样式。** 模块作者碰不到 CSS，风格也就漂不了。
//
// 两档面板在这里汇合：
//
//   A 档（声明里有 live=）：拖控件时在浏览器本地跑算子，实测 2 ms 级，
//       和平台自己的特殊处理一样跟手。停手后再取一次后端的全分辨率结果。
//   B 档（没有 live=）：本地算不了，松手才打后端。这不是降级 ——
//       平台自己的膜厚模块（FFT）本来就是这么做的。
//
// 两档都会在图注里**如实标注**你现在看到的是抽样预览还是全分辨率结果。

import { h, mount, fmtInt, fmtNum, errorBox, skeletonRows } from '../ui.js';
import { xyChart } from '../chart.js';
import { heatmap } from './heatmap.js';
import { figure, bandControl, numberControl, selectControl } from './figure.js';
import { infoDot, withInfo } from './info.js';
import { downloadMenu } from '../download.js';
import { runOp } from '../ops.js';

// 停手多久之后去取精确值。和 sample.js 里那个 300 ms 一致 ——
// 两处都是「拖动中不打后端」的同一个决定。
const SETTLE_MS = 300;

// 后端没给高度时的兜底。正常情况下 Panel.height 总会带过来（默认也是 250）。
// **这是真实像素** —— 自从 chart.js 的 viewBox 跟着容器宽度走，写多少就是多少。
const CHART_H = 250;

/**
 * 渲染一个模块。
 *
 * @param host    挂载点
 * @param spec    后端给的模块声明（ModuleSpec.as_dict()）
 * @param opts
 *   - `frames`   已载入的抽样谱 {lambda, time, values, ...}，A 档本地计算要用
 *   - `compute`  (params) => Promise<{panel_id: {x, y, label, y_label, caption}}>
 *                后端精确计算
 *   - `sampleName` 下载文件名用
 */
export function moduleView(host, spec, { frames, compute, sampleName = '样品' }) {
  const params = { ...Object.fromEntries(
    (spec.controls || []).map((c) => [c.key, c.default])) };

  const hosts = {};          // panel_id → 画图的容器
  let settleTimer = 0;
  // 上一次取精确值之后又动过哪些控件。带给后端，它据此判断哪些面板要重算 ——
  // `uses=[]` 的面板（比如膜厚那格全波段对照）结果不可能变，
  // 陪着算一遍就是白白多等几十毫秒。
  let changed = new Set();

  // ── 控件画在哪一格：第一个 uses 到它的面板。声明里没人 uses 的控件
  //    放在第一格 —— 不显示出来的话，用户就永远改不了它。
  const ownerOf = (key) => {
    const p = (spec.panels || []).find((x) => (x.uses || []).includes(key));
    return p ? p.id : (spec.panels || [])[0]?.id;
  };

  function controlsFor(panelId) {
    const out = [];
    for (const c of spec.controls || []) {
      if (ownerOf(c.key) !== panelId) continue;
      out.push(buildControl(c));
    }
    return out;
  }

  function buildControl(c) {
    const live = () => { changed.add(c.key); paintLocal(c.key); scheduleExact(); };
    const commit = () => { changed.add(c.key); scheduleExact(0); };

    // 声明说「跟着波长/时间轴」就用数据的实际范围。写死一个数只对一台仪器成立。
    const axis = (kind) => {
      if (!frames) return null;
      if (kind === 'lambda') return [frames.lambda[0], frames.lambda[frames.lambda.length - 1]];
      if (kind === 'time') return [frames.time[0], frames.time[frames.time.length - 1]];
      return null;
    };

    if (c.type === 'band') {
      const [lo, hi] = c.default || [0, 1];
      // 波段天然是波长上的一段，默认就跟着波长轴走
      const [min, max] = axis(c.range_from || 'lambda') || [lo, hi];
      return bandControl(min, max, () => params[c.key],
        (a, b) => { params[c.key] = [a, b]; live(); }, commit,
        { label: c.label, unit: c.unit || 'nm', step: c.step ?? 1 });
    }
    if (c.type === 'number') {
      const auto = axis(c.range_from);
      const min = auto ? auto[0] : (c.min ?? 0);
      const max = auto ? auto[1] : (c.max ?? 1e6);
      return numberControl(c.label, params[c.key], min, max,
        c.step ?? 1, (v) => { params[c.key] = v; live(); commit(); });
    }
    if (c.type === 'select') {
      return selectControl(c.label, params[c.key],
        (c.options || []).map((o) => [o, String(o)]),
        (v) => { params[c.key] = v; live(); commit(); });
    }
    if (c.type === 'bool') {
      return h('label.inline-field',
        h('input', { type: 'checkbox', checked: !!params[c.key],
          onchange: (e) => { params[c.key] = e.target.checked; live(); commit(); } }),
        h('span.small.muted', c.label));
    }
    // 认不出来的类型不该静默忽略 —— 那样用户会以为控件坏了
    return h('span.xsmall.danger', `认不出的控件类型：${c.type}`);
  }

  // ── A 档：本地跑算子，立刻重画
  //
  // `changed` 是刚动过的那个控件的 key。**只重算真正依赖它的面板** ——
  // 无脑全画一遍的话，拖一个滑块会把另一格也算一遍，白干一倍的活。
  // （实测：全画 3.4 ms，只画相关的 2.2 ms，和迁移前一致。）
  function paintLocal(changed = null) {
    if (!frames) return;
    for (const p of spec.panels || []) {
      if (!p.live) continue;                 // B 档没有本地路径
      if (changed && !Object.values(p.live.bind || {}).includes(changed)) continue;
      let y;
      try {
        y = runOp(p.live.op, frames, resolveArgs(p.live, params));
      } catch (err) {
        mount(hosts[p.id], errorBox(err));
        continue;
      }
      draw(p, frames.time, y, false);
    }
  }

  // ── 两档共用：停手后取后端的精确结果
  function scheduleExact(delay = SETTLE_MS) {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(async () => {
      const sending = [...changed];
      changed = new Set();
      // B 档在等的时候要有交代，不能干瞪眼。
      //
      // 但**只给这次真会重算的那几格挂骨架屏**。后端按面板声明的 uses
      // 跳过输入没变的格子（膜厚那格全波段 FFT 就是这么省下 165 ms 的），
      // 被跳过的格子不会有新数据回来 —— 提前清空的话，它就永远停在骨架屏上，
      // 屏幕上少一张图。省下来的时间不值这个。
      for (const p of spec.panels || []) {
        if (p.live || !hosts[p.id]) continue;
        if (sending.length && !(p.uses || []).some((k) => sending.includes(k))) continue;
        mount(hosts[p.id], skeletonRows(3));
      }
      let data;
      try {
        // 第一次（sending 为空）不带 changed，后端就当全都要算
        data = await compute(params, sending.length ? sending : null);
      } catch (err) {
        sending.forEach((k) => changed.add(k));    // 失败了别把「动过」这件事丢掉
        for (const p of spec.panels || []) {
          if (!p.live && hosts[p.id]) mount(hosts[p.id], errorBox(err, () => scheduleExact(0)));
        }
        return;      // A 档保留本地预览，不打断
      }
      for (const p of spec.panels || []) {
        const d = data[p.id];
        // 后端没回的面板 = 它的输入没变，保持屏幕上那一份，别清空
        if (d) draw(p, d.x, d.y, true, d);
      }
    }, delay);
  }

  /**
   * 这一格第一次画出来时淡入一下，之后再也不淡。
   *
   * 拖控件时每一帧都会走 draw()，那条路上一帧都不能多花 ——
   * 而且拖着拖着曲线一直在闪烁地淡入，那不是精致，是烦人。
   */
  function firstPaint(host) {
    if (!host || host.__drawn) return;
    host.__drawn = true;
    host.classList.add('enter');
    // 跑完就把类摘掉。留着的话每一格身上会一直挂着一个已完成的动画对象，
    // 「拖动时有没有动画在跑」这种检查就再也问不清楚了。
    host.addEventListener('animationend', () => host.classList.remove('enter'),
                          { once: true });
  }

  /**
   * 画一格。三种 kind 各走各的，但**图注、提示块、stats 是共用的** ——
   * 那些是「这一格在说什么」，跟画的是曲线还是位图无关。
   */
  function draw(panel, x, y, exact, d = null) {
    const host = hosts[panel.id];
    if (!host) return;

    firstPaint(host);
    if (panel.kind === 'text') return drawText(host, panel, d);
    if (panel.kind === 'heatmap') return drawHeatmap(host, panel, d);

    // ── kind === 'xy'
    // 后端把单条也归一成了 series，本地预览走的是 x/y —— 两条路在这里合流
    const series = d?.series?.length
      ? d.series.map((sr) => ({ label: sr.label, x: sr.x, y: sr.y,
                                style: sr.style, color: sr.color || undefined }))
      : [{ label: d?.label || panel.title, x, y, style: 'line' }];

    const chartSpec = {
      x_label: panel.x_label || '时间 (s)',
      y_label: d?.y_label || panel.y_label || '',
      series,
    };
    host.__spec = chartSpec;
    const nPts = Math.max(...series.map((sr) => sr.x?.length || 0), 0);
    mount(host,
      xyChart(chartSpec, { height: panel.height || CHART_H, width: host.clientWidth }),
      caption(panel, d, exact, nPts),
      noticeBox(d));
  }

  function drawText(host, panel, d) {
    // 规范报告这种整段要看的东西。**不折叠** —— 折起来就等于没给。
    host.__spec = null;
    mount(host,
      h('pre.code-block.is-half', d?.text || ''),
      caption(panel, d, true, null),
      noticeBox(d));
  }

  function drawHeatmap(host, panel, d) {
    host.__spec = null;
    host.__imageUrl = d?.image_url || '';
    if (!d?.image_url) { mount(host, skeletonRows(3)); return; }
    const [x0, x1] = d.x_range || [0, 1];
    const [y0, y1] = d.y_range || [0, 1];
    const [v0, v1] = d.v_range || [0, 1];
    mount(host,
      // 位图 + 矢量坐标轴，复用平台自己那个组件 —— 二维数据当位图传，不塞进 json
      heatmap({
        src: d.image_url,
        xMin: x0, xMax: x1, yMin: y0, yMax: y1,
        vMin: v0, vMax: v1, vLabel: d.v_label || '',
        xLabel: panel.x_label || '时间 (s)', yLabel: d.y_label || panel.y_label || '',
        height: panel.height || CHART_H, cmap: d.cmap || 'gray',
        width: host.clientWidth,
      }),
      caption(panel, d, true, null),
      noticeBox(d));
  }

  /** 图注：预览/精确的标注 + stats 那串带 ⓘ 的数字 + 文字说明。三种 kind 共用。 */
  function caption(panel, d, exact, nPts) {
    const bits = [];
    // 只有**真会出现预览态**的面板才标这一句。
    //
    // 它存在的意义是分清「你现在看的是拖动时的抽样预览」和「松手后的精确结果」。
    // B 档面板压根没有预览态，永远是精确的 —— 那句「全分辨率 · 211 点」
    // 什么都没说清，只是把图注前面那格位置占掉了。
    if (panel.kind === 'xy' && panel.live) {
      bits.push(exact
        ? h('span.status.status-ok.xsmall', `全分辨率 · ${fmtInt(nPts)} 点`)
        : h('span.status.status-accent.xsmall',
            `实时预览 · λ 抽样至 ${fmtNum(frames?.lambda_step, 3)} nm`));
    }
    for (const st of d?.stats || []) {
      const text = `${st.label} ${typeof st.value === 'number'
        ? fmtNum(st.value, Number.isInteger(st.value) ? 0 : undefined) : st.value}`
        + (st.unit ? ` ${st.unit}` : '');
      bits.push(h('span.xsmall' + (st.tone ? `.status.status-${st.tone}` : ''),
        '　', st.info ? withInfo(text, st.info) : text));
    }
    // 面板的 ⓘ 附加段跟着当前数据走，挂在图注末尾那个 ⓘ 上
    if (d?.info_extra && panel.info) {
      bits.push(infoDot(panel.info, { extra: d.info_extra }));
    }
    const extraCap = [d?.caption, panel.caption].filter(Boolean).join('　');
    if (extraCap) bits.push(h('span.xsmall.dim', `　${extraCap}`));
    return bits.length ? h('div.chart-caption', ...bits) : null;
  }

  /**
   * 面板的提示块。**画在图下面，不是上面。**
   *
   * 放上面的话它把这一格的图往下推三行，而同排另一格没有提示块 ——
   * 两张图的顶就错开一百来像素，一眼就看出来是歪的。
   * 平台自己那版膜厚页当年就是为这个把它挪下去的，迁移时别再挪回来。
   */
  function noticeBox(d) {
    const n = d?.notice;
    if (!n) return null;
    // 「这一格是对照，不是测量结果」这种必须看得见，塞图注会被当脚注忽略
    return h('div.notice' + (n.kind && n.kind !== 'info' ? `.notice-${n.kind}` : ''),
      h('div.grow',
        n.title ? h('div.small.strong', n.title) : null,
        n.body ? h('p.xsmall.dim.mt-2', n.body) : null));
  }

  // ── 搭骨架
  const cols = spec.columns === 1 ? '' : `.fig-grid-${spec.columns || 2}`;

  // 独占一整行的面板（报告这种）不进网格，跟在后面。
  // 塞进 2 列网格里的话，它旁边会空出半行，而且把那一带的行高一起撑高。
  const inGrid = (spec.panels || []).filter((p) => p.span !== 1);
  const fullWidth = (spec.panels || []).filter((p) => p.span === 1);

  const buildFigure = (p) => {
    const body = h('div.module-panel-body', skeletonRows(3));
    hosts[p.id] = body;
    return figure(p.info ? withInfo(p.title, p.info) : p.title, {
      head: p.kind === 'text' ? null : downloadMenu({
        // 热力图是服务端渲染的位图，下的就是那张图；曲线下 SVG / CSV
        svg: () => (p.kind === 'heatmap' ? null : body.querySelector('svg')),
        spec: () => (p.kind === 'heatmap' ? null : body.__spec || null),
        imageUrl: p.kind === 'heatmap' ? () => body.__imageUrl || '' : null,
        name: `${sampleName}_${p.title}`,
      }),
      ctl: controlsFor(p.id),
      body,
    });
  };

  mount(host,
    inGrid.length ? h(`div${cols}`, ...inGrid.map(buildFigure)) : null,
    ...fullWidth.map((p) => h('div.mt-4', buildFigure(p))));

  paintLocal();          // A 档立刻有东西看
  scheduleExact(0);      // 两档都去取一次精确值
  return { params, refresh: () => { paintLocal(); scheduleExact(0); } };
}

/** 把 `{算子参数: 控件 key}` 解成 `{算子参数: 实际值}`。和后端 _resolve_bind 同一件事。 */
function resolveArgs(live, params) {
  const out = {};
  for (const [arg, key] of Object.entries(live.bind || {})) out[arg] = params[key];
  return out;
}
