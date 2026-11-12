// 流畅性测量（C078）：改之前、改之后各跑一遍，比数字，不靠「看起来顺」。
//   NODE_PATH=$(npm root -g) node tools/qa/perf_check.js run_xxx smp_xxx [轮数=5]
// 量三样：
//   1. 点侧栏 / 点进下钻页 → 内容出来（活动页里没有骨架屏、有正文）要多久（中位数，ms）
//   2. 切页、开弹窗、开 AI 抽屉那一段的帧时间（rAF 间隔的 p95 和最差一帧，ms）—— 动画不能拿卡顿换
//   3. 第二次进同一页时有没有闪骨架屏（旧内容先留着）
const { chromium } = require('playwright');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = 'http://127.0.0.1:8799/';
const med = (xs) => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };

async function frames(p, act, ms = 700) {
  await p.evaluate(() => {
    window.__f = []; let last = performance.now();
    const tick = (t) => { window.__f.push(t - last); last = t; if (window.__f.length < 400) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  });
  await act();
  await sleep(ms);
  const f = await p.evaluate(() => window.__f.slice(1));
  const s = [...f].sort((a, b) => a - b);
  return { n: f.length, p95: +s[Math.floor(s.length * 0.95)].toFixed(1), worst: +s[s.length - 1].toFixed(1),
           over20: f.filter((x) => x > 20).length };
}

async function ready(p, page) {
  await p.waitForFunction((id) => {
    const el = document.querySelector(`#page-${id}.is-active`);
    return el && !el.querySelector('.skeleton') && el.textContent.trim().length > 20;
  }, page, { timeout: 20000 });
}

(async () => {
  const [run, sid, roundsArg] = process.argv.slice(2);
  const rounds = Number(roundsArg || 5);
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  await p.goto(BASE + '#overview', { waitUntil: 'networkidle' }); await sleep(800);

  // 1. 点侧栏
  const navT = {};
  for (const id of ['process', 'storage', 'relation', 'history', 'overview']) navT[id] = [];
  for (let r = 0; r < rounds; r += 1) {
    for (const id of Object.keys(navT)) {
      const t0 = Date.now();
      await p.click(`.nav-item[data-page="${id}"]`);
      await ready(p, id);
      navT[id].push(Date.now() - t0);
      await sleep(250);
    }
  }
  console.log('点侧栏 → 内容出来（中位数 ms）', Object.fromEntries(Object.entries(navT).map(([k, v]) => [k, med(v)])));

  // 2. 下钻：对比历史 → 批处理结果；样品页
  const drill = { batch: [], sample: [] };
  for (let r = 0; r < Math.min(rounds, 3); r += 1) {
    await p.click('.nav-item[data-page="history"]'); await ready(p, 'history'); await sleep(200);
    let t0 = Date.now();
    await p.click(`#page-history tr[data-run="${run}"] td:first-child`);
    await p.waitForFunction(() => document.querySelector('#page-batch.is-active #chartHost svg, #page-batch.is-active #chartHost canvas'),
                            null, { timeout: 20000 });
    drill.batch.push(Date.now() - t0);
    t0 = Date.now();
    await p.evaluate((s) => { location.hash = `sample/${s}`; }, sid);
    await ready(p, 'sample');
    drill.sample.push(Date.now() - t0);
    await sleep(300);
  }
  console.log('下钻 → 内容出来（中位数 ms）', { batch: med(drill.batch), sample: med(drill.sample) });

  // 3. 帧时间
  await p.click('.nav-item[data-page="overview"]'); await ready(p, 'overview'); await sleep(400);
  const fNav = await frames(p, () => p.click('.nav-item[data-page="storage"]'));
  await ready(p, 'storage'); await sleep(300);
  const fBack = await frames(p, () => p.click('.nav-item[data-page="overview"]'));
  await p.click('.nav-item[data-page="process"]'); await ready(p, 'process'); await sleep(500);
  const fModal = await frames(p, () => p.click('#page-process button:has-text("存为样品集")'));
  await p.keyboard.press('Escape'); await sleep(500);
  const fDrawer = await frames(p, () => p.click('#aiToggle'));
  await p.click('#aiToggle').catch(() => {}); await sleep(400);
  console.log('帧时间 ms（p95 / 最差 / 超过 20ms 的帧）',
    { 切页: fNav, 切回: fBack, 开弹窗: fModal, 开AI抽屉: fDrawer });

  // 4. 再进一次：有没有闪骨架屏
  await p.click('.nav-item[data-page="storage"]'); await ready(p, 'storage'); await sleep(300);
  await p.click('.nav-item[data-page="overview"]'); await ready(p, 'overview'); await sleep(300);
  const flash = await p.evaluate(() => new Promise((res) => {
    let seen = false;
    const host = document.querySelector('#page-storage');
    const mo = new MutationObserver(() => { if (host.querySelector('.skeleton')) seen = true; });
    mo.observe(host, { childList: true, subtree: true });
    document.querySelector('.nav-item[data-page="storage"]').click();
    setTimeout(() => { mo.disconnect(); res(seen); }, 1500);
  }));
  console.log('再进存储页闪了骨架屏：', flash);
  await b.close();
})();
