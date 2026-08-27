// 图和数据的下载。
//
// 一张图看完总要带走 —— 要么贴进 PPT，要么拿数据自己画。两个出口都给：
//
// - **下载图片**：SVG 图表就地栅格化成 2× PNG；服务端渲染的热力图直接取那张 PNG。
// - **下载数据**：CSV。图上画的是什么，导出来就是什么，不重新采样、不平滑。
//
// 栅格化那一步有个不显眼的坑：SVG 里的颜色全是 `var(--s1)` 这种，
// 一旦脱离页面就没有值了，画出来是一片黑。所以序列化之前要把**算好的**
// 颜色逐个写死成属性。

import { h, toast } from './ui.js';

/** 触发一次浏览器下载。 */
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 立刻 revoke 会让某些浏览器的下载半路断掉，等一拍
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---------------------------------------------------------------- 图片
// 这些属性带 var()，脱离页面就解析不出来了，必须逐个算好写死
const PAINT = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap',
               'opacity', 'fill-opacity', 'stroke-opacity',
               'font-family', 'font-size', 'font-weight', 'text-anchor'];

function inlineStyles(src, dst) {
  const a = src.querySelectorAll('*');
  const b = dst.querySelectorAll('*');
  for (let i = 0; i < a.length; i++) {
    const cs = getComputedStyle(a[i]);
    for (const prop of PAINT) {
      const v = cs.getPropertyValue(prop);
      if (v && v !== 'none' || prop === 'fill') b[i].setAttribute(prop, v);
    }
    b[i].removeAttribute('class');
  }
}

/** SVG 节点 → 2× PNG。失败时退回 SVG 文件（照样能用，只是不能直接贴图）。 */
async function savePng(svg, basename) {
  const rect = svg.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width));
  const hgt = Math.max(1, Math.round(rect.height));

  const clone = svg.cloneNode(true);
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('width', w);
  clone.setAttribute('height', hgt);
  inlineStyles(svg, clone);

  // 背景要自己铺白/铺深 —— SVG 本身是透明的，贴进 PPT 会看到底色透出来
  const bg = getComputedStyle(document.body).backgroundColor || '#ffffff';
  const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bgRect.setAttribute('width', '100%');
  bgRect.setAttribute('height', '100%');
  bgRect.setAttribute('fill', bg);
  clone.insertBefore(bgRect, clone.firstChild);

  const source = new XMLSerializer().serializeToString(clone);
  const svgBlob = new Blob([source], { type: 'image/svg+xml;charset=utf-8' });

  try {
    const png = await rasterize(svgBlob, w, hgt);
    saveBlob(png, `${basename}.png`);
  } catch {
    // 栅格化被拦下来（比如图里引了外部资源），至少把矢量图给出去
    saveBlob(svgBlob, `${basename}.svg`);
    toast('转 PNG 失败，已改为下载 SVG', 'warn');
  }
}

function rasterize(svgBlob, w, hgt, scale = 2) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(svgBlob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const canvas = h('canvas');
      canvas.width = w * scale;
      canvas.height = hgt * scale;
      const cx = canvas.getContext('2d');
      cx.scale(scale, scale);
      cx.drawImage(img, 0, 0, w, hgt);
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob 返回空'))), 'image/png');
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('SVG 载入失败')); };
    img.src = url;
  });
}

// ---------------------------------------------------------------- 数据
const csvCell = (v) => {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

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

// ---------------------------------------------------------------- 按钮
/**
 * 「下载 ▾」按钮，点开两个子项：下载图片 / 下载数据(csv)。
 *
 * @param {object} o
 * @param {() => SVGElement|null} o.svg     取当前的图节点（图会重画，所以传函数不传节点）
 * @param {() => object|null}     o.spec    取当前的图表 spec，用来出 CSV
 * @param {() => string|null}     [o.imageUrl] 服务端已经渲染好的图（热力图走这条）
 * @param {string}                o.name    文件名主干
 */
export function downloadMenu({ svg, spec, imageUrl = null, name }) {
  const btn = h('button.btn.btn-sm.btn-ghost', { title: '下载这张图或它的数据' }, '下载 ▾');

  btn.addEventListener('click', () => {
    const items = [
      ['下载图片', async () => {
        const url = imageUrl?.();
        if (url) return saveBlob(await (await fetch(url)).blob(), `${name}.png`);
        const node = svg?.();
        if (!node) return toast('图还没画出来', 'warn');
        await savePng(node, name);
      }],
      ['下载数据（CSV）', () => {
        const s = spec?.();
        const csv = s ? seriesToCsv(s) : '';
        if (!csv) return toast('这张图没有可导出的数据', 'warn');
        saveBlob(new Blob([csv], { type: 'text/csv;charset=utf-8' }), `${name}.csv`);
      }],
    ];

    const menu = h('div.ai-menu',
      ...items.map(([label, fn]) => h('button.ai-menu-item', {
        onclick: async () => {
          close();
          try { await fn(); } catch (err) { toast(`下载失败：${err.message}`, 'danger'); }
        },
      }, label)));

    const r = btn.getBoundingClientRect();
    menu.style.top = `${r.bottom + 4}px`;
    // 靠右对齐，免得贴着窗口右边的图把菜单顶出屏幕
    menu.style.left = `${Math.max(8, r.right - 180)}px`;
    document.body.appendChild(menu);

    const close = () => {
      menu.remove();
      document.removeEventListener('pointerdown', away, true);
    };
    const away = (e) => { if (!menu.contains(e.target) && e.target !== btn) close(); };
    setTimeout(() => document.addEventListener('pointerdown', away, true), 0);
  });

  return btn;
}
