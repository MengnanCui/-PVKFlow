// A2 验收：每一条都给一个能量出来的数。同一份脚本先跑旧前端、再跑新前端。
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8799/';
const out = [];
const rec = (id, ok, detail) => { out.push({ id, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${id.padEnd(30)} ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fresh(b, hash = 'overview') {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  p.errs = [];
  p.on('pageerror', (e) => p.errs.push(e.message));
  p.on('console', (m) => m.type() === 'error' && p.errs.push(m.text()));
  await p.goto(BASE + '#' + hash, { waitUntil: 'networkidle' });
  await sleep(300);
  return { ctx, p };
}
const count = (p, pred) => { const c = { n: 0 }; p.on('request', (q) => { if (pred(q)) c.n++; }); return c; };

(async () => {
  const b = await chromium.launch();
  const [sid, rid] = process.argv.slice(2);

  // #4 一次点击渲染几遍
  { const { ctx, p } = await fresh(b);
    const c = count(p, (q) => q.url().includes('/api/selection/query'));
    await p.click('.nav-item[data-page="process"]'); await sleep(1500);
    rec('#4 导航只渲染一次', c.n === 1, `点一次「数据处理」，/selection/query 调了 ${c.n} 次`);
    await ctx.close(); }

  // #5 来回进出数据处理页之后，拖一个文件传几遍
  { const { ctx, p } = await fresh(b, 'process');
    for (let i = 0; i < 3; i++) {
      await p.click('.nav-item[data-page="overview"]'); await sleep(250);
      await p.click('.nav-item[data-page="process"]'); await sleep(400);
    }
    const c = count(p, (q) => q.url().includes('/api/files/upload'));
    await p.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File([`x,y\n1,${Math.random()}\n`], `drop_${Date.now()}.csv`, { type: 'text/csv' }));
      document.querySelector('#page-process').dispatchEvent(
        new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    await sleep(1500);
    rec('#5 拖拽只上传一遍', c.n === 1, `进出 4 次后拖 1 个文件，上传了 ${c.n} 遍`);
    await ctx.close(); }

  // #6 跑着批处理离开对比页，标题会不会被改
  { const { ctx, p } = await fresh(b, 'process');
    await p.click('#runBatchBtn');
    await p.waitForURL(/#batch\/task_/, { timeout: 8000 });
    await p.click('.nav-item[data-page="overview"]');
    await sleep(5000);
    const title = await p.textContent('#pageTitle');
    const hash = await p.evaluate(() => location.hash);
    rec('#6 离开后不改别页标题', title === '总览' && hash === '#overview', `离开 5 s 后：标题「${title}」、地址 ${hash}`);
    await ctx.close(); }

  // #18 清空搜索框之后，路上的旧结果不许冒出来
  { const { ctx, p } = await fresh(b, 'storage');
    await p.route('**/api/selection/query', async (route) => {
      if ((route.request().postData() || '').includes('ZG0013')) await sleep(1500);
      await route.continue();
    });
    const input = await p.$('input[type=search]');
    await input.fill('ZG0013'); await sleep(450);       // 防抖过了、请求在路上
    await input.fill(''); await p.dispatchEvent('input[type=search]', 'input');
    await sleep(2200);
    const txt = (await p.textContent('#storageSearchResult')).trim();
    rec('#18 旧搜索结果不回魂', txt === '', `清空后 2 s 结果区：「${txt.slice(0, 30)}」`);
    await ctx.close(); }

  // #19 拖数字滑块时打几次后端
  { const { ctx, p } = await fresh(b, 'sample/' + sid);
    await sleep(2500);
    const c = count(p, (q) => q.url().includes('/compute') && q.url().includes('special.slope_integral'));
    const n = await p.evaluate(async () => {
      const sec = [...document.querySelectorAll('section.module')].find((s) => s.textContent.includes('谱斜率'));
      const r = sec?.querySelector('input.range');
      if (!r) return -1;
      for (let i = 0; i < 20; i++) {
        r.value = String(900 + i * 2);
        r.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((res) => setTimeout(res, 20));       // 像真的拖：每帧 ~20 ms
      }
      r.dispatchEvent(new Event('change', { bubbles: true }));  // 松手
      return 20;
    });
    await sleep(1500);
    rec('#19 拖动不逐帧打后端', n === 20 && c.n <= 2, `拖 20 帧再松手，compute 请求 ${c.n} 次`);
    await ctx.close(); }

  // #20 样品页下载的文件名
  { const { ctx, p } = await fresh(b, 'sample/' + sid);
    await sleep(2500);
    const btn = (await p.$$('button:has-text("下载 ▾")'))[1];
    await btn.click(); await sleep(200);
    const [dl] = await Promise.all([p.waitForEvent('download', { timeout: 5000 }),
      p.click('text=下载数据（CSV）')]);
    const name = dl.suggestedFilename();
    rec('#20 下载文件名带样品名', name.startsWith('ZG00'), `下载文件名：${name}`);
    await ctx.close(); }

  // #21 `#/settings` 这种写法
  { const { ctx, p } = await fresh(b);
    await p.evaluate(() => { location.hash = '#/settings'; }); await sleep(800);
    const title = await p.textContent('#pageTitle');
    rec('#21 #/settings 进设置页', title === '设置', `打开 #/settings 后标题「${title}」`);
    await ctx.close(); }

  // #22 曲线加载出错后「重试」
  { const { ctx, p } = await fresh(b);
    let failOnce = true;
    await p.route('**/curves?*column=integral*', async (route) => {
      if (failOnce) { failOnce = false; await route.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ error: { message: '故意失败一次', kind: 'internal', detail: '' } }) }); }
      else await route.continue();
    });
    await p.evaluate((r) => { location.hash = '#batch/' + r; }, rid); await sleep(2500);
    const hadErr = (await p.textContent('#chartHost')).includes('故意失败一次');
    await p.click('#chartHost button:has-text("重试")'); await sleep(2000);
    const stillErr = (await p.textContent('#chartHost')).includes('故意失败一次');
    rec('#22 曲线重试真的重来', hadErr && !stillErr, `先报错=${hadErr}，点重试后仍报错=${stillErr}`);
    await ctx.close(); }

  // #27 对比页说明里的星号
  { const { ctx, p } = await fresh(b, 'batch/' + rid);
    await sleep(2000);
    const txt = await p.textContent('#recipeHost');
    rec('#27 没有原样的 **', !txt.includes('**') && txt.includes('下面「不同时刻'), txt.includes('**') ? '仍有 **' : '无 **，写的是「下面」');
    await ctx.close(); }

  // #7 AI「用这个范围继续问」之后，下一问带的范围
  { const { ctx, p } = await fresh(b, 'process');
    const bodies = [];
    p.on('request', (q) => { if (q.url().includes('/messages') && q.method() === 'POST') bodies.push(JSON.parse(q.postData())); });
    await p.click('#aiToggle'); await sleep(600);
    await p.fill('textarea.ai-input', '请帮我收窄范围'); await p.press('textarea.ai-input', 'Enter');
    await p.waitForSelector('button:has-text("用这个范围继续问")', { timeout: 8000 });
    await p.click('button:has-text("用这个范围继续问")'); await sleep(800);
    await p.fill('textarea.ai-input', '这几个怎么样'); await p.press('textarea.ai-input', 'Enter');
    await sleep(1500);
    const last = bodies[bodies.length - 1]?.scope?.filter || {};
    rec('#7 收窄范围真的生效', JSON.stringify(last.batch) === '["ZG0014"]', `第二问带的范围：${JSON.stringify(last)}`);
    await ctx.close(); }

  // #23 流式回答途中开新对话
  { const { ctx, p } = await fresh(b, 'process');
    await p.click('#aiToggle'); await sleep(600);
    await p.fill('textarea.ai-input', '慢慢说'); await p.press('textarea.ai-input', 'Enter');
    await sleep(900);
    await p.click('.ai-head button[title="菜单"]'); await sleep(200);
    await p.click('text=新对话'); await sleep(800);
    const bodyTxt = (await p.textContent('.ai-body')).trim();
    const btnTxt = await p.textContent('.ai-foot button.btn-sm, .ai-compose button.btn-sm').catch(() => null);
    const sendBtn = await p.evaluate(() => [...document.querySelectorAll('#aiDrawer button')].map((x) => x.textContent).find((t) => t === '发送' || t === '停止'));
    rec('#23 换对话就停掉旧回答', !bodyTxt.includes('段。') && sendBtn === '发送',
        `新对话里出现旧回答的字=${bodyTxt.includes('段。')}，按钮=「${sendBtn}」`);
    await ctx.close(); }

  // 设置页：存完马上离开，会不会被拽回去
  { const { ctx, p } = await fresh(b, 'settings');
    await sleep(800);
    await p.click('button:has-text("重载")'); await sleep(50);
    await p.click('.nav-item[data-page="overview"]'); await sleep(1200);
    const title = await p.textContent('#pageTitle');
    rec('设置页不再拽回', title === '总览', `点「重载」后马上去总览，1.2 s 后标题「${title}」`);
    await ctx.close(); }

  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
})();
