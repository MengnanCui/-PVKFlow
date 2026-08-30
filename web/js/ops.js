// 算子集的 JS 一侧。**和 app/modules/ops.py 是一对，必须给出相同的数。**
//
// 拖控件时跑的是这一份（本地，实测 2.2 ms）；批处理时跑的是 Python 那一份。
// 模块作者只是引用算子名，两份实现都不是他写的 —— 这正是他能白拿
// 「拖动即时响应」而又不用碰 JS 的原因。
//
// ⚠️ 改这个文件必须同时改 app/modules/ops.py，并且 tests/test_ops.py
//    会拿同一个矩阵跑两边、逐点比对（容差 1e-9）。
//    只改一边的后果是：界面上拖出来的数和存进库里的数不一样，而且没有任何提示 ——
//    这是最难发现的那种错。
//
// 实现本身是从 web/js/spectra.js 搬过来的，不是重写的（那两个函数一直
// 就是双实现里的 JS 那一半，只是以前没有名字）。

/** 某波段的积分 vs 时间。梯形法 —— 换边界时曲线连续，不会因为多算少算一个点而跳变。 */
export function bandIntegral(lam, V, nT, { band }) {
  const [lo, hi] = band;
  const idx = [];
  for (let i = 0; i < lam.length; i++) if (lam[i] >= lo && lam[i] <= hi) idx.push(i);
  if (idx.length < 2) return new Array(nT).fill(null);

  const out = new Array(nT).fill(0);
  for (let n = 0; n < idx.length - 1; n++) {
    const a = idx[n], b = idx[n + 1];
    const dx = lam[b] - lam[a];
    const ra = V[a], rb = V[b];
    for (let j = 0; j < nT; j++) out[j] += 0.5 * (ra[j] + rb[j]) * dx;
  }
  return out;
}

/** 某波长处的谱斜率 dI/dλ vs 时间。窗口内最小二乘拟合，比两点差分抗噪。 */
export function wavelengthSlope(lam, V, nT, { center, half }) {
  const idx = [];
  for (let i = 0; i < lam.length; i++) {
    if (lam[i] >= center - half && lam[i] <= center + half) idx.push(i);
  }
  if (idx.length < 3) return new Array(nT).fill(null);

  let mean = 0;
  for (const i of idx) mean += lam[i];
  mean /= idx.length;
  let denom = 0;
  for (const i of idx) denom += (lam[i] - mean) ** 2;
  if (denom === 0) return new Array(nT).fill(null);

  const out = new Array(nT).fill(0);
  for (const i of idx) {
    const xc = lam[i] - mean;
    const row = V[i];
    for (let j = 0; j < nT; j++) out[j] += xc * row[j];
  }
  for (let j = 0; j < nT; j++) out[j] /= denom;
  return out;
}

// 名字必须和 app/modules/ops.py 里的 OPS 一一对应
export const OPS = {
  band_integral: bandIntegral,
  wavelength_slope: wavelengthSlope,
};

/**
 * 按名字跑一个算子。
 *
 * @param name   算子名，见 OPS
 * @param frames {lambda, time, values} —— 平台已经载入的抽样谱
 * @param args   已经从控件值解出来的参数，如 {band:[800,950]} 或 {center:950,half:10}
 */
export function runOp(name, frames, args) {
  const fn = OPS[name];
  if (!fn) {
    // 界面上不该走到这儿 —— 模块装进来之前验证器就该拦住了。
    // 真走到了说明前后端的算子集不同步，说清楚比静默画一条空曲线好。
    throw new Error(`前端没有这个算子：${name}。`
      + `现有的：${Object.keys(OPS).join(', ')}。`
      + '多半是 web/js/ops.js 和 app/modules/ops.py 不同步了。');
  }
  return fn(frames.lambda, frames.values, frames.time.length, args);
}
