// 前端侧的光谱整理：选时刻叠谱、认出饱和的短波端。
//
// 拖控件时实时算的那些（波段积分、谱斜率）在 ops.js —— 那里每个算子都有
// Python 的同名实现，tests/test_ops.py 用 node 逐点比对两边。原来这个文件里
// 还有一份积分 / 斜率的副本，没人调用，2026-11 删掉了：两份前端实现只会让人
// 不知道该改哪一份。
//
// 注意：膜厚的 FFT 提取**只在后端做**。同一段物理有两份实现，一旦漂移
// 就分不清该信哪个数。

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
