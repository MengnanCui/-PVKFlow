// 图表数据 → CSV。不碰 DOM：tests/test_frontend_csv.py 直接在 node 里跑它。

/**
 * 一格。
 *
 * **公式注入**：Excel / WPS 打开 CSV 时，以 `= + - @`、制表符、回车开头的格子会被当成公式执行
 * （`=HYPERLINK(...)`、`=cmd|...` 这一类）。曲线名来自文件夹名和样品名 —— 是别人给的数据。
 * 这种文字格子前面加一个 `'`，Excel 就把它当纯文字显示（自查 §6 #29）。
 * 只动文字：数（包括负数 -0.5）原样写，不然一列数就全变成文字了。
 */
export function csvCell(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : '';
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s) && !Number.isFinite(Number(s))) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const sameX = (series) => series.length > 1 && series.every((s) =>
  s.x.length === series[0].x.length && s.x.every((v, i) => v === series[0].x[i]));

/**
 * 图表 spec → CSV。
 *
 * 各条曲线共用同一条 x 轴时用宽表（一列 x + 每条一列），
 * 否则用长表（series,x,y）—— 宽表在 x 不一致时只能靠补齐或插值，
 * 那等于凭空造数。宁可换个形状，也不改数。
 */
export function seriesToCsv(spec) {
  const series = (spec.series || []).filter((s) => s && s.x && s.y);
  if (!series.length) return '';
  const xl = spec.x_label || 'x';
  const yl = spec.y_label || 'y';
  const lines = [];

  if (series.length === 1 || sameX(series)) {
    lines.push([xl, ...series.map((s) => s.label || yl)].map(csvCell).join(','));
    for (let i = 0; i < series[0].x.length; i++) {
      lines.push([series[0].x[i], ...series.map((s) => s.y[i])].map(csvCell).join(','));
    }
  } else {
    lines.push(['series', xl, yl].map(csvCell).join(','));
    for (const s of series) {
      for (let i = 0; i < s.x.length; i++) {
        lines.push([s.label || '', s.x[i], s.y[i]].map(csvCell).join(','));
      }
    }
  }
  // BOM：不加的话 Excel 打开中文列名是乱码
  return '﻿' + lines.join('\n') + '\n';
}

/** 表头 + 行 → CSV（带 BOM，Excel 打开中文不乱码）。每一格都过 csvCell。 */
export function rowsToCsv(header, rows) {
  const lines = [header, ...rows].map((r) => r.map(csvCell).join(','));
  return '\ufeff' + lines.join('\n') + '\n';
}
