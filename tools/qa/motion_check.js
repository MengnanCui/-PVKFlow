// C078 验收：动效和「再进一页旧内容先留着」之后可能冒出来的问题，逐条量。
//   NODE_PATH=$(npm root -g) node tools/qa/motion_check.js run_xxx
const { chromium } = require('playwright');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = 'http://127.0.0.1:8799/';
const out = [];
const rec = (name, ok, detail) => { out.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`); };

(async () => {
  const [run] = process.argv.slice(2);
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));
  p.on('console', (m) => { if (m.type() === 'error' && !/aborted|AbortError/i.test(m.text())) errs.push(m.text()); });
  await p.goto(BASE + '#overview', { waitUntil: 'networkidle' }); await sleep(500);

  // 1. 连点：动画还没放完就点下一个，最后停在最后点的那一页，只有一页是活动的
  const seq = ['process', 'storage', 'relation', 'history', 'overview', 'storage', 'history'];
  for (const id of seq) { await p.click(`.nav-item[data-page="${id}"]`, { delay: 0 }); await sleep(30); }
  await sleep(1500);
  const active = await p.$$eval('.page.is-active', (xs) => xs.map((x) => x.id));
  const title = await p.textContent('#pageTitle');
  const lit = await p.$eval('.nav-item[aria-current="page"]', (x) => x.dataset.page);
  rec('连点 7 下', active.length === 1 && active[0] === 'page-history' && title === '对比历史' && lit === 'history',
      `活动页 ${active.join(',')}、标题「${title}」、侧栏亮 ${lit}`);

  // 2. 方向：点进下钻页 forward、返回 back、侧栏换页 swap
  await p.click(`#page-history tr[data-run="${run}"] td:first-child`); await sleep(100);
  const d1 = await p.evaluate(() => document.documentElement.dataset.nav);
  await sleep(1200);
  await p.click('#pageActions .back-btn'); await sleep(100);
  const d2 = await p.evaluate(() => document.documentElement.dataset.nav);
  await p.click('.nav-item[data-page="storage"]'); await sleep(100);
  const d3 = await p.evaluate(() => document.documentElement.dataset.nav);
  const anim = await p.$eval('#page-storage', (x) => getComputedStyle(x).animationName);
  rec('切页方向', d1 === 'forward' && d2 === 'back' && d3 === 'swap' && anim === 'page-in-swap',
      `进对比页 ${d1}、返回 ${d2}、换侧栏 ${d3}（存储页动画 ${anim}）`);

  // 3. 弹窗：关的那一刻就不算开着；关了马上再开，只有一个；退场的那个最多 300 ms 后从页面上拿掉
  await p.click('.nav-item[data-page="process"]'); await sleep(1500);
  const btn = '#page-process button:has-text("存为样品集")';
  await p.click(btn); await sleep(400);
  const anim2 = await p.$eval('.modal', (x) => getComputedStyle(x).animationName);
  await p.keyboard.press('Escape');
  const right = await p.evaluate(() => [document.querySelectorAll('.modal').length,
    document.querySelectorAll('.modal-leaving').length, document.activeElement?.textContent?.trim()]);
  await p.click(btn); await sleep(60);
  const both = await p.evaluate(() => [document.querySelectorAll('.modal').length, document.querySelectorAll('.modal-leaving').length]);
  await sleep(400);
  const later = await p.evaluate(() => document.querySelectorAll('.modal-leaving,.modal-backdrop-leaving').length);
  await p.keyboard.press('Escape'); await sleep(400);
  rec('弹窗开关', anim2 === 'sheet-in' && right[0] === 0 && right[1] === 1 && right[2] === '存为样品集'
      && both[0] === 1 && later === 0,
      `进场 ${anim2}；Esc 一下：开着的 ${right[0]}、退场中 ${right[1]}、焦点回到「${right[2]}」；马上再开：开着的 ${both[0]}；400 ms 后残留 ${later}`);

  // 4. 弹窗里再弹确认框，Esc 只关上面那层
  await p.goto(BASE + '#history', { waitUntil: 'networkidle' }); await sleep(600);
  await p.click(`#page-history tr[data-run="${run}"] button:has-text("改名")`); await sleep(300);
  await p.keyboard.press('Escape'); await sleep(350);
  const afterEsc = await p.$$eval('.modal', (xs) => xs.length);
  rec('Esc 关弹窗', afterEsc === 0, `改名框 Esc 后还开着 ${afterEsc} 个`);

  // 5. 再进一次：旧内容先留着、新数据到了原地换掉（改了名之后回来，看到的是新名字）
  await p.click('.nav-item[data-page="overview"]'); await sleep(600);
  const old = (await p.evaluate(async (r) => (await (await fetch(`/api/batch/runs/${r}`)).json()).run.params.title, run));
  await p.evaluate(async ([r, t]) => fetch(`/api/batch/runs/${r}`, { method: 'PATCH', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title: t }) }), [run, 'QA 刷新测试']);
  await p.click('.nav-item[data-page="history"]');
  const immediately = await p.$eval('#page-history', (x) => [!!x.querySelector('.skeleton'), x.classList.contains('is-refreshing')]);
  await sleep(1200);
  const name = (await p.textContent(`#page-history tr[data-run="${run}"] .run-name`)).trim();
  const settled = await p.$eval('#page-history', (x) => [x.classList.contains('is-refreshing'), x.getAttribute('aria-busy')]);
  await p.evaluate(async ([r, t]) => fetch(`/api/batch/runs/${r}`, { method: 'PATCH', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title: t }) }), [run, old]);
  rec('再进一页先留旧内容', !immediately[0] && name === 'QA 刷新测试' && !settled[0] && !settled[1],
      `点进去那一刻：骨架屏 ${immediately[0]}、刷新中 ${immediately[1]}；1.2 s 后名字「${name}」、刷新线 ${settled[0]}、aria-busy ${settled[1]}`);

  // 6. 系统设了「减少动态效果」：动画时长压到 0
  const rm = await b.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const q = await rm.newPage();
  await q.goto(BASE + '#overview', { waitUntil: 'networkidle' }); await sleep(400);
  await q.click('.nav-item[data-page="storage"]'); await sleep(50);
  const dur = await q.$eval('#page-storage', (x) => getComputedStyle(x).animationDuration);
  await q.click('.nav-item[data-page="process"]'); await sleep(1200);
  await q.click('#page-process button:has-text("存为样品集")'); await sleep(50);
  const mdur = await q.$eval('.modal', (x) => getComputedStyle(x).animationDuration);
  await q.keyboard.press('Escape'); await sleep(100);
  const gone = await q.$$eval('.modal-leaving', (xs) => xs.length);
  rec('减少动态效果', parseFloat(dur) < 0.01 && parseFloat(mdur) < 0.01 && gone === 0,
      `切页动画 ${dur}、弹窗 ${mdur}、关了 100 ms 后残留 ${gone}`);

  rec('零报错', errs.length === 0, errs.slice(0, 3).join(' | ') || '无');
  await b.close();
  console.log(`\n${out.filter(Boolean).length} / ${out.length} 通过`);
})();
