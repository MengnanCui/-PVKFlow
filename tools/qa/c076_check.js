// C076 验收：共用组件之后，分段控件认方向键；指标卡 / 字段表还在；离开页面时请求真的被中止。
//   NODE_PATH=$(npm root -g) node tools/qa/c076_check.js run_xxx（一次批处理，最好样品多一点）
const { chromium } = require('playwright');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(20)} ${detail}`); };

(async () => {
  const [run] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));
  p.on('console', (m) => { if (m.type() === 'error' && !/aborted|AbortError/i.test(m.text())) errs.push(m.text()); });

  // 1. 智能体页：「模式」分段控件，Tab 落在选中的那个，→ 换到下一个且真的生效
  await p.goto('http://127.0.0.1:8799/#agent', { waitUntil: 'networkidle' }); await sleep(600);
  const seg = '#page-agent .seg[aria-label="模式"]';
  const before = await p.$eval(`${seg} [aria-checked="true"]`, (x) => x.textContent);
  await p.focus(`${seg} [aria-checked="true"]`);
  await p.keyboard.press('ArrowRight'); await sleep(300);
  const after = await p.$eval(`${seg} [aria-checked="true"]`, (x) => x.textContent);
  const focused = await p.evaluate(() => document.activeElement?.getAttribute('aria-checked'));
  const tabs = await p.$$eval(`${seg} button`, (bs) => bs.map((x) => x.tabIndex));
  await p.keyboard.press('ArrowLeft'); await sleep(300);
  const back = await p.$eval(`${seg} [aria-checked="true"]`, (x) => x.textContent);
  rec('分段控件方向键', before !== after && focused === 'true' && back === before && tabs.filter((t) => t === 0).length === 1,
      `${before} →（右）${after} →（左）${back}；可 Tab 进入的 ${tabs.filter((t) => t === 0).length} 个`);

  // 2. 总览 / 存储：指标卡和字段表（共用组件）
  await p.goto('http://127.0.0.1:8799/#overview', { waitUntil: 'networkidle' }); await sleep(500);
  const ov = await p.$$eval('#page-overview .metric', (xs) => xs.length);
  await p.goto('http://127.0.0.1:8799/#storage', { waitUntil: 'networkidle' }); await sleep(500);
  const st = await p.$$eval('#page-storage .metric', (xs) => xs.length);
  const cols = await p.$$eval('#page-storage table.data thead th', (xs) => xs.map((x) => x.textContent).join('|'));
  rec('指标卡 / 字段表', ov === 4 && st === 4 && cols.includes('其中数值'), `总览 ${ov} 张、存储 ${st} 张；字段表「${cols}」`);

  // 3. 进对比页马上离开：曲线请求被中止（不是等它下完）
  const aborted = [];
  p.on('requestfailed', (r) => { if (r.url().includes('/curves')) aborted.push(r.failure()?.errorText); });
  await p.goto(`http://127.0.0.1:8799/#batch/${run}`); await sleep(120);
  await p.click('.nav-item[data-page="overview"]'); await sleep(1500);
  const title = await p.textContent('#pageTitle');
  rec('离开页面中止请求', aborted.length > 0 && title === '总览', `中止 ${aborted.length} 个曲线请求（${aborted[0] || '—'}），标题「${title}」`);

  rec('零报错', errs.length === 0, errs.slice(0, 3).join(' | ') || '无');
  await b.close();
  console.log(`\n${out.filter(Boolean).length} / ${out.length} 通过`);
})();
