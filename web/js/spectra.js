// 前端侧的派生曲线计算。
//
// 这两个函数是 app/parsers/render.py 里 band_integral / wavelength_slope 的
// 逐行对照实现 —— 拖滑块时在本地算（0.1 ms 级），松手不需要等后端。
// 两边必须给出相同结果：tests 里用 /api/spectra/{id}/curve 做交叉验证。
//
// 注意：膜厚的 FFT 提取**只在后端做**。同一段物理有两份实现，一旦漂移
// 就分不清该信哪个数。

/** frames: {lambda:[L], time:[T], values:[L][T]} */
export function bandIntegral(frames, lo, hi) {
  const { lambda: lam, time: t, values: V } = frames;
  const idx = [];
  for (let i = 0; i < lam.length; i++) if (lam[i] >= lo && lam[i] <= hi) idx.push(i);
  if (idx.length < 2) return t.map(() => null);

  const out = new Array(t.length).fill(0);
  // 梯形法。用求和而不是矩形法，换波段边界时曲线才连续、不会因为多算
  // 少算一个采样点而跳变。
  for (let n = 0; n < idx.length - 1; n++) {
    const a = idx[n], b = idx[n + 1];
    const dx = lam[b] - lam[a];
    const ra = V[a], rb = V[b];
    for (let j = 0; j < t.length; j++) out[j] += 0.5 * (ra[j] + rb[j]) * dx;
  }
  return out;
}

export function wavelengthSlope(frames, center, halfWidth) {
  const { lambda: lam, time: t, values: V } = frames;
  const idx = [];
  for (let i = 0; i < lam.length; i++) {
    if (lam[i] >= center - halfWidth && lam[i] <= center + halfWidth) idx.push(i);
  }
  if (idx.length < 3) return t.map(() => null);

  // 窗口内最小二乘拟合斜率，比两点差分抗噪
  let mean = 0;
  for (const i of idx) mean += lam[i];
  mean /= idx.length;
  let denom = 0;
  for (const i of idx) denom += (lam[i] - mean) ** 2;
  if (denom === 0) return t.map(() => null);

  const out = new Array(t.length).fill(0);
  for (const i of idx) {
    const xc = lam[i] - mean;
    const row = V[i];
    for (let j = 0; j < t.length; j++) out[j] += xc * row[j];
  }
  for (let j = 0; j < t.length; j++) out[j] /= denom;
  return out;
}

/** 取某几个时刻的谱，每条按自身最大值归一化。 */
export function spectraAtTimes(frames, times, normalize = true) {
  const { lambda: lam, time: t, values: V } = frames;
  return times.map((target) => {
    let j = 0, best = Infinity;
    for (let n = 0; n < t.length; n++) {
      const d = Math.abs(t[n] - target);
      if (d < best) { best = d; j = n; }
    }
    const y = new Array(lam.length);
    let max = -Infinity;
    for (let i = 0; i < lam.length; i++) {
      y[i] = V[i][j];
      if (y[i] > max) max = y[i];
    }
    if (normalize && max > 0) for (let i = 0; i < y.length; i++) y[i] /= max;
    return { label: `${t[j].toFixed(2)} s`, actual: t[j], x: lam, y, style: 'line' };
  });
}

/**
 * 窗口的频率分辨率 —— 纯几何量，不依赖膜厚算法。
 *
 * 相位对波数线性（δ = 2π·OPD·k），所以窗口跨越的 Δk 决定了能分辨的
 * 最小光程差。选波段前先看这两个数，可以避免选一个根本测不出来的窗口。
 */
export function windowResolution(lamMin, lamMax, minCycles = 1.5) {
  if (!(lamMin > 0) || !(lamMax > lamMin)) return null;
  const dk = 1 / lamMin - 1 / lamMax;          // nm⁻¹
  const binF = 1 / dk;                          // 一个频率 bin 对应的光程差 (nm)
  return {
    dk,
    binF,
    otFloor: (minCycles * binF) / 2,            // 可测的最小光学厚度 (nm)
    minCycles,
  };
}

/** 已知光学厚度时，窗内有几条纹。用来判断某个波段够不够用。 */
export function cyclesFor(otNm, lamMin, lamMax) {
  const r = windowResolution(lamMin, lamMax);
  return r ? (2 * otNm) * r.dk : null;
}


/**
 * 短波端的饱和区从哪里结束。
 *
 * 光源在紫外端没有输出，仪器会把值顶到量程两端（真实文件里就是一串
 * 0 和 100）。这段数据不是信号，但它参与「按自身最大值归一化」的话，
 * 会把真正的谱压扁到图的下半部分 —— 一张全是噪声的图。
 *
 * 判据：某条波长上有超过 60% 的帧恰好落在全局最小或最大值上，就算饱和。
 * 从短波端连续数过去，第一条不饱和的波长就是可用区的起点。
 */
export function saturatedHead(frames, threshold = 0.6) {
  const { lambda: lam, values: V } = frames;
  if (!lam?.length) return { index: 0, lambda: null, count: 0 };

  let lo = Infinity, hi = -Infinity;
  for (const row of V) {
    for (let j = 0; j < row.length; j++) {
      const v = row[j];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!(hi > lo)) return { index: 0, lambda: null, count: 0 };

  let i = 0;
  for (; i < lam.length; i++) {
    const row = V[i];
    let pinned = 0;
    for (let j = 0; j < row.length; j++) {
      if (row[j] === lo || row[j] === hi) pinned++;
    }
    if (pinned / row.length < threshold) break;
  }
  return { index: i, lambda: i < lam.length ? lam[i] : null, count: i };
}
