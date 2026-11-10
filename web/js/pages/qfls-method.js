// QFLS 方法说明：每个指标怎么算、阈值是多少、分数怎么来、不能怎么用。
//
// 阈值从这次处理的 run.json 里读（处理时用的是哪套就显示哪套）；还没处理过就读平台当前的默认值。
// 这里写的口径和 app/qfls/metrics.py 一一对应 —— 改了那边，这里的数字跟着变，文字要人改。

import { api } from '../api.js';
import { h, mount, skeletonRows, errorBox } from '../ui.js';
import { S } from './qfls-state.js';
import { limits } from './qfls-views.js';

export function methodTab() {
  const host = h('div');
  if (S.run?.params) mount(host, body(S.run.params, `这次处理（${S.runId}）用的参数`));
  else {
    mount(host, skeletonRows(6));
    api.qflsMethod().then((m) => mount(host, body(m.params, '平台当前的默认参数（还没有处理过）')))
      .catch((err) => mount(host, errorBox(err)));
  }
  return host;
}

const pct = (v) => `${+(v * 100).toFixed(2)}%`;
const mev = (v) => `${+(v * 1000).toFixed(1)} meV`;

function sec(title, ...kids) {
  return h('div.section', h('div.section-head', h('div.section-title', title)), h('div.panel.panel-body.qfls-method', ...kids));
}
const p = (...kids) => h('p', ...kids);
const li = (...kids) => h('li', ...kids);

// 早期的 run.json 里扣分表只有数字（没有 metric / label / unit），按当时写死的七项补上
const LEGACY = {
  spread: ['p90_p10', '离散 P90 − P10', 'eV'], low_frac: ['low_2s_frac', '低于中位数 − 2σ 占比', 'frac'],
  low_area: ['low_cc_max_frac', '最大低值连通域占画面', 'frac'], scratch: ['scratch_count', '划痕候选', '条'],
  edge: ['edge_minus_center', '|边缘 − 中心|', 'eV', true], moran: ['morans_i', "Moran's I", ''],
  bright: ['bright_density', '亮点密度', '个/百万像素'],
};

function fmtBy(unit, v) {
  if (unit === 'eV') return mev(v);
  if (unit === 'frac') return pct(v);
  return unit ? `${v} ${unit}` : String(v);
}

// 扣分表一行：完全照 run.json 里的 params.score 来（后端 app/qfls/metrics.py 的 PARAMS["score"]）
function scoreRow(code, t) {
  const [metric, label, unit, abs] = LEGACY[code] || [];
  const m = t.metric || metric || code;
  const u = t.unit ?? unit ?? '';
  const name = (t.abs ?? abs) ? `|${m}|` : m;
  const per = 'per' in t;
  return h('tr', h('td', t.label || label || code), h('td', h('code', name)),
    h('td', per ? '每个' : fmtBy(u, t.from)), h('td', per ? `${t.per} 分` : fmtBy(u, t.to)), h('td.num', String(t.max)));
}

function body(P, source) {
  const grid = P.moran_grid;
  return h('div',
    limits(),
    h('p.small.muted.mt-3', `下面的数字是${source}。所有阈值都是平台定的（登记在计划文档 D-162 … D-177），不是标准；`,
      '标「你给的」的是你的要求原文。'),

    sec('1 · 数据怎么认',
      p('根目录 → 实验文件夹 → 批次哈希目录 → slot1、slot2… → point1、point2… → ', h('code', 'QFLS/'), ' + 点位同名 ', h('code', 'pointN.qvdf'),
        '。slot / point 数量不固定；根目录下认不出这种层级的（备份、说明、别的实验类型）列进「跳过」并写原因。'),
      p('QFLS 目录里只取 ', h('code', '0'), ' 开头的 ', h('code', '.txt'), '（', h('code', '0-物镜X-405R-光强-时间戳-uuid.txt'),
        '），同目录的历史分析产物一律不碰。物镜、光强、时间戳、uuid 从文件名拆；qvdf 里写的物镜和文件名对不上时按文件名算并标出来。'),
      p('中文路径的乱码（GBK 被当成别的编码解）只在显示时修成 UTF-8，磁盘上的名字一个字不动。'),
      p(h('strong', '原始数据只读'), '：所有输出写进工作区 ', h('code', 'qfls/<处理编号>/'), ' 一个独立目录，处理前后原始目录里每个文件的内容和修改时间都不变（测试守着）。')),

    sec('2 · 每一帧先校验（不过就不打分）',
      h('ul',
        li('UTF-8（可带 BOM）、逗号分隔、无表头的浮点矩阵，单位 eV'),
        li(h('strong', '行列一致'), '：每一行的列数都一样'),
        li(h('strong', '无缺失'), '：没有空字段、NaN、inf、认不出的字符'),
        li(h('strong', '范围合理'), `：${P.valid_range_ev[0]} – ${P.valid_range_ev[1]} eV 以外的像素超过 ${pct(P.invalid_out_of_range_frac)} 整帧无效；`,
          '更少的话用中位数填补后再算空间指标，并标「超范围」')),
      p('无效帧没有分数、不进排名和分布，只在表里列出无效原因。行列数可以和别的帧不同（采集配置会变），但会标「尺寸不同」—— 按像素计的面积、数量类指标在不同尺寸之间不可比。')),

    sec('3 · 指标',
      h('table.data', h('thead', h('tr', h('th', '指标'), h('th', '怎么算'))), h('tbody',
        ...[
          ['均值 / 中位数 / 标准差', '全部像素（eV）。σ 用整帧标准差'],
          ['P90 − P10、P99 − P1', '分位数之差，离散程度（显示为 meV）'],
          ['边缘 − 中心', `边缘带（宽 = 短边的 ${pct(P.edge_band_frac)}）的均值 − 中心区（中间 ${pct(P.center_frac)} × ${pct(P.center_frac)}）的均值；负数 = 边缘更低`],
          ['低荧光绝对占比', `低于 ${P.low_abs_ev} eV 的像素占比。绝对阈值 —— 不同物镜之间不可比`],
          ['低于中位数 − 2σ / 高于中位数 + 2σ 占比', `相对阈值（${P.sigma_k}σ）。纯随机噪声时各约 2.3%`],
          ['低值连通域数量与最大面积', `先 ${P.smooth_px} × ${P.smooth_px} 均值平滑，低于中位数 − ${P.sigma_k}σ 的像素按 ${P.connectivity} 连通分块，≥ ${P.low_cc_min_px} 像素才算；最大面积给像素数、占画面比例，有像素尺寸时给 µm²`],
          ['划痕候选数', `低值连通域里：面积 ≥ ${P.scratch_min_px} 像素、长短轴比 ≥ ${P.scratch_min_elong}、长度 ≥ 短边的 ${pct(P.scratch_min_len_frac)}、宽度 ≤ ${P.scratch_max_width_px} 像素、质心不在边缘带里（长短轴来自连通域的二阶矩）`],
          ['亮点密度', `高于中位数 + ${P.bright_k}σ 的连通域里面积 ${P.bright_min_px} – ${P.bright_max_px} 像素的个数，按「个 / 百万像素」`],
          ["Moran's I", `把图分成 ${grid[0]} × ${grid[1]} 块取块均值，相邻块（上下左右）权重 1：I = (N / ΣW) · Σ wᵢⱼ zᵢ zⱼ / Σ zᵢ²。≈ 0 随机，> ${P.moran_threshold} 提示结构不均（你给的）。逐像素算的话光学模糊让相邻像素天然相关，I 永远接近 1，所以在块上算`],
          ['变异函数变程', `先 ${P.variogram_bin} × ${P.variogram_bin} 合并像素；γ(h) = ½·E[(Z(x+h) − Z(x))²]，横竖两个方向平均，h 到短边的 ${pct(P.variogram_max_lag_frac)}；基台 = 方差，变程 = γ 第一次到 ${pct(P.variogram_sill_frac)} 基台的距离（线性插值）。没到 = 有大尺度趋势，记空`],
        ].map(([a, b]) => h('tr', h('td.strong.nowrap', a), h('td.small', b)))))),

    sec('4 · 综合质量分与等级',
      p(`综合分 = 100 − ${Object.keys(P.score).length} 项扣分（每项封顶，线性插值）。`, h('strong', '绝对 QFLS 水平不进分数'), '（不同物镜不可比）—— 分数只看「均不均匀、有没有缺陷」。'),
      h('table.data', h('thead', h('tr', h('th', '扣分项'), h('th', '看哪个指标'), h('th', '从…开始扣'), h('th', '到…扣满'), h('th.num', '最多扣'))),
        h('tbody', ...Object.entries(P.score).map(([code, t]) => scoreRow(code, t)))),
      p(`等级：≥ ${P.grades.A} 为 A、≥ ${P.grades.B} 为 B、≥ ${P.grades.C} 为 C、其余 D；无效帧没有等级。`)),

    sec('5 · 异常标记',
      h('ul',
        li(h('span.tag.tag-danger', 'error'), ' 数据无效（行列不一致、缺失、超范围过多、读不出来）'),
        li(h('span.tag.tag-warn', 'warn'), ` 超范围像素、文件名不规范、物镜与 qvdf 不符、大面积低值（最大一块 > ${pct(P.flag_large_low_frac)} 画面）、划痕、`,
          `结构不均（Moran's I > ${P.moran_threshold}）、边缘效应（|边缘 − 中心| > ${mev(P.flag_edge_ev)}）、`,
          `平行点离群（同一 slot、同一物镜至少 ${P.flag_group_min_n} 个点时，中位数的稳健 z = (x − 中位数) / (1.4826·MAD) 的绝对值 > ${P.flag_group_robust_z}）、整帧数值不变`),
        li(h('span.tag', 'info'), ' 缺 qvdf、qvdf 名字不对、尺寸和多数帧不同、采集条件和多数不同、变异函数没到基台')),
      p('有 warn 或 error 的样点算「异常」，在排名表里整行标色。同一实验里不同 slot 通常是不同处理，所以离群只在平行点之间判断。')),

    sec('6 · 分组、标准化、排名',
      p('组 = 实验 × 物镜。组内 z = (x − 组均值) / 组标准差，组里少于 3 个有效点时为空。跨物镜想比的话看组内 z（它说的是「在自己那组里偏高还是偏低」，不是绝对值）。'),
      p('排名给两种：全局（按综合分）和物镜内。多点对比的热图在同一物镜内用同一个色标（该物镜所有帧 P1 的第 5 百分位到 P99 的第 95 百分位）。')),

    sec('7 · 不做什么（你给的限制）',
      h('ul',
        li('不同物镜的绝对 QFLS 和面积占比不直接比较'),
        li('只有单一光强（1 sun）：不做伪 JV、理想因子、PCE、FF —— 这些要多个光强的 QFLS 才能推'),
        li("Moran's I ≈ 0 为随机、> 0.15 提示结构不均"),
        li('中文路径的 GBK 乱码统一按 UTF-8 显示'))),

    sec('8 · 演示数据',
      p('「生成演示数据」造的 60 帧全部是假想数据（app/qfls/demo.py）：3 个实验（退火温度、钝化剂、衬底）、12 种情形；暗斑、划痕、亮点、边缘衰减、倾斜、大尺度不均都是按设定画上去的，',
        '100X 故意比 20X 低 24 meV 来体现「物镜之间不可比」；4 帧故意做坏，一个实验目录名故意做成乱码。它只用来看平台能不能正确识别、算、标出来，',
        h('strong', '不代表任何真实材料的结论'), '。')));
}
