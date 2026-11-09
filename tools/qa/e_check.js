// E 组（构效关系第一版）的验收。
//
// 准备（只对测试工作区）：先跑 tools/qa/seed_relation.py 写入合成字段 qa_x / qa_y。
//   NODE_PATH=$(npm root -g) node tools/qa/e_check.js
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8799/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`); };
const r3 = (v) => (Math.abs(v) >= 0.9995 ? v.toFixed(4) : v.toFixed(3));

async function api(p, path, body) {
  return p.evaluate(async ([path, body]) => (await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })).json(), [path, body]);
}

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#relation', { waitUntil: 'networkidle' });
  await sleep(1000);
  await p.evaluate(() => { try { localStorage.removeItem('hte.relation.choice'); } catch { /* */ } });
  await p.reload({ waitUntil: 'networkidle' }); await sleep(1000);

  // 1. 选 qa_x / qa_y：点数、统计数字都和接口一致
  // 页面是常驻的（切走只是隐藏），别的页上也有 .figure —— 一律限定在构效关系页里
  const R = '#page-relation';
  const sel = (i) => p.locator(`${R} .figure-ctl select`).nth(i);
  await sel(0).selectOption('qa_x'); await sleep(800);
  await sel(1).selectOption('qa_y'); await sleep(1200);
  const ref = await api(p, '/api/relation/pairs', { x: 'qa_x', y: 'qa_y', filter: {} });
  const nDots = await p.evaluate(() => document.querySelector('#page-relation .figure svg g[stroke-width="1.2"]')?.childElementCount ?? -1);
  rec('散点一个样品一个点', nDots === ref.points.length, `图上 ${nDots} 个点，接口 ${ref.points.length} 个样品`);
  const text = await p.textContent(`${R} .relation-stats`);
  const want = [r3(ref.stats.pearson.r), r3(ref.stats.spearman.rho), `${ref.points.length} 个样品`];
  rec('统计数字和接口一致', want.every((w) => text.includes(w)), `页面上有 ${want.join(' / ')}`);
  const title = await p.textContent(`${R} .figure-title`);
  rec('标题跟着字段走', title.includes('QA 合成 Y') && title.includes('QA 合成 X'), `「${title.trim()}」`);

  // 2. 交换 X / Y
  await p.click(`${R} button[title="交换 X 和 Y"]`); await sleep(1000);
  const title2 = await p.textContent(`${R} .figure-title`);
  rec('交换 X / Y', title2.indexOf('QA 合成 X') < title2.indexOf('QA 合成 Y'), `「${title2.trim()}」`);
  await p.click(`${R} button[title="交换 X 和 Y"]`); await sleep(1000);

  // 3. 悬停看到样品名，点一下打开样品
  const svg = p.locator(`${R} .figure svg`).first();
  const pt = await p.evaluate(() => {
    const c = document.querySelector('#page-relation .figure svg g[stroke-width="1.2"] circle');
    const r = c.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await p.mouse.move(pt.x, pt.y); await sleep(300);
  const tip = await svg.evaluate((s) => [...s.querySelectorAll('text')].map((t) => t.textContent).join(' | '));
  rec('悬停看得到是哪个样品', tip.includes('点一下打开这个样品') && ref.points.some((q) => tip.includes(q.name)), tip.slice(0, 80));
  await p.mouse.click(pt.x, pt.y); await sleep(1200);
  const h1 = await p.evaluate(() => location.hash);
  rec('点点打开样品', h1.startsWith('#sample/'), `地址 ${h1.slice(0, 26)}`);
  await p.goBack(); await sleep(1500);

  // 4. 范围跟着数据处理页：在那边点一个分面，这边选「筛出的」
  await p.click('.nav-item[data-page="process"]'); await sleep(1200);
  await p.click('#page-process button.chip-toggle:not([disabled])'); await sleep(1000);
  const flt = await p.evaluate(async () => (await import('/assets/js/scope.js')).getScope().filter);
  await p.click('.nav-item[data-page="relation"]'); await sleep(1500);
  await sel(2).selectOption('filter'); await sleep(1200);
  const refF = await api(p, '/api/relation/pairs', { x: 'qa_x', y: 'qa_y', filter: flt });
  const text2 = await p.textContent(`${R} .relation-stats`);
  rec('范围用数据处理页的筛选', refF.points.length < ref.points.length && text2.includes(`${refF.points.length} 个样品`),
      `筛选 ${JSON.stringify(flt)} → ${refF.points.length} 个（全部 ${ref.points.length}）`);
  await sel(2).selectOption('all'); await sleep(1000);

  // 5. 逐个样品的表：键盘也能打开样品
  await p.click(`${R} details summary:has-text("逐个样品")`); await sleep(200);
  const nRows = await p.$$eval(`${R} details[open] table tbody tr`, (xs) => xs.length);
  await p.focus(`${R} details[open] table tbody button.linklike`);
  await p.keyboard.press('Enter'); await sleep(1200);
  const h2 = await p.evaluate(() => location.hash);
  rec('表格行数、键盘打开', nRows === ref.points.length && h2.startsWith('#sample/'), `${nRows} 行；回车后 ${h2.slice(0, 26)}`);
  await p.goBack(); await sleep(1500);

  // 6. 下载数据：每个样品一行，带样品名和批次
  await p.locator(`${R} .figure`).first().locator('button:has-text("下载")').click(); await sleep(200);
  const [dl] = await Promise.all([p.waitForEvent('download'), p.click('button.ai-menu-item:has-text("下载数据")')]);
  const csv = fs.readFileSync(await dl.path(), 'utf8').replace(/^﻿/, '').trim().split('\n');
  rec('下载的 CSV', csv.length === ref.points.length + 1 && csv[0].startsWith('样品,批次'), `${csv.length} 行，表头「${csv[0]}」`);

  // 7. 箱线图：组数和接口一致；离群点（用一份造好的响应）悬停有名字、点了打开样品
  const refG = await api(p, '/api/relation/groups', { y: 'qa_y', filter: {} });
  const nBoxes = await p.evaluate(() => document.querySelectorAll('#page-relation .figure')[1]?.querySelectorAll('svg rect').length);
  rec('箱线图一组一个箱', nBoxes === refG.groups.length, `${nBoxes} 个箱，接口 ${refG.groups.length} 组`);
  const fake = {
    y: refG.y, by: 'batch', n: 6,
    groups: [{ name: 'QA组', n: 6, min: 1, q1: 2, median: 3, q3: 4, max: 50, mean: 10, whisker_lo: 1, whisker_hi: 5,
               outliers: [{ sample_id: ref.points[0].sample_id, name: '离群的那片', y: 50 }] }],
  };
  await p.route('**/api/relation/groups', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fake) }));
  await p.locator(`${R} .figure`).nth(1).locator('select').selectOption('qa_x'); await sleep(1000);
  const ob = await p.evaluate(() => {
    const c = document.querySelectorAll('#page-relation .figure')[1].querySelector('svg circle');
    const r = c.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await p.mouse.move(ob.x, ob.y); await sleep(300);
  const tip2 = await p.evaluate(() => [...document.querySelectorAll('#page-relation .figure')[1].querySelectorAll('svg text')].map((t) => t.textContent).join('|'));
  const listed = await p.evaluate(() => document.querySelectorAll('#page-relation .figure')[1].textContent.includes('离群的那片（QA组）'));
  rec('离群点有名字、列出来', tip2.includes('离群的那片') && listed, `悬停「${tip2.slice(0, 40)}」，下方列出=${listed}`);
  await p.mouse.click(ob.x, ob.y); await sleep(1200);
  const h3 = await p.evaluate(() => location.hash);
  rec('点离群点打开样品', h3.startsWith('#sample/'), `地址 ${h3.slice(0, 26)}`);
  await p.unroute('**/api/relation/groups');

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
