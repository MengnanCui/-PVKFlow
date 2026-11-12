// B（二）的验收：拆页面、删死代码、AI 抽屉和 ⓘ 共用一个聊天客户端之后，行为没变。
//
// 用法（准备工作见 README.md；设置里要配**两个**模型 fake-1 / fake-2，模型下拉才会出现）：
//   NODE_PATH=$(npm root -g) node tools/qa/b2_check.js smp_xxx run_xxx /tmp/requests.jsonl
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8799/';
const [sid, rid, reqLog] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(26)} ${detail}`); };

async function fresh(b, hash, init) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  p.errors = [];
  p.on('console', (m) => { if (m.type() === 'error') p.errors.push(m.text()); });
  p.on('pageerror', (e) => p.errors.push('PAGEERR ' + e.message));
  if (init) await p.addInitScript(init);
  await p.goto(BASE + '#' + hash, { waitUntil: 'networkidle' });
  return { ctx, p };
}

(async () => {
  const b = await chromium.launch();

  // 1. 每一页打开都没有报错
  { const { ctx, p } = await fresh(b, 'overview');
    const routes = ['overview', 'process', 'sample/' + sid, 'batch/' + rid, 'history', 'storage', 'relation', 'settings'];
    const bad = [];
    for (const r of routes) {
      const n = p.errors.length;
      await p.evaluate((h) => { location.hash = '#' + h; }, r); await sleep(1500);
      if (p.errors.length > n) bad.push(`${r}: ${p.errors.slice(n).join(' | ').slice(0, 160)}`);
    }
    rec('八个页面零报错', bad.length === 0, bad.length ? bad.join(' ;; ') : `${routes.length} 页，console 无 error`);
    await ctx.close(); }

  // 2. 对比页（拆成五个文件）：四块都画出来了，改时间窗会重新切片，导出对话框能开
  { const { ctx, p } = await fresh(b, 'batch/' + rid);
    await sleep(2500);
    const parts = await p.evaluate(() => ({
      slices: document.querySelectorAll('#sliceHost svg').length,
      chart: document.querySelectorAll('#chartHost svg').length,
      recipe: document.querySelectorAll('#recipeHost input, #recipeHost select').length,
      table: document.querySelectorAll('#tableHost tr').length,
    }));
    rec('对比页四块都在', parts.slices > 0 && parts.chart > 0 && parts.recipe > 0 && parts.table > 1,
        `切片图 ${parts.slices}、叠图 ${parts.chart}、配方控件 ${parts.recipe}、表格行 ${parts.table}`);
    let sliceCalls = 0;
    p.on('request', (q) => { if (q.url().includes('/slices')) sliceCalls += 1; });
    const num = await p.$('#sliceHost input[type="number"]');
    if (num) { await num.fill('5'); await num.press('Enter'); await sleep(1500); }
    rec('改时间窗重新切片', !!num && sliceCalls >= 1, num ? `改窗口后 /slices 请求 ${sliceCalls} 次` : '没找到时间窗输入框');
    await p.click('#chartHost button:has-text("导出脚本")'); await sleep(500);
    const dlg = await p.textContent('.modal .modal-head h3').catch(() => null);
    rec('导出对话框', dlg === '导出绘图脚本', `弹窗标题「${dlg}」`);
    rec('对比页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // 3. 数据处理页（拆成五个文件，经 hooks 回调）：拖选、点分面、直接跑一次对比
  { const { ctx, p } = await fresh(b, 'process');
    await sleep(1200);
    const rows = await p.$$('.vrow.is-pickable');
    const box0 = await rows[0].boundingBox(); const box2 = await rows[2].boundingBox();
    await p.mouse.move(box0.x + 200, box0.y + box0.height / 2); await p.mouse.down();
    await p.mouse.move(box0.x + 200, box0.y + box0.height * 1.5, { steps: 4 });
    await p.mouse.move(box2.x + 200, box2.y + box2.height / 2, { steps: 4 }); await p.mouse.up();
    await sleep(300);
    const label = await p.textContent('#selectCount');
    const sel = await p.$$eval('.vrow[aria-selected="true"]', (xs) => xs.length);
    rec('拖选三行', label === '已选 3' && sel === 3, `计数「${label}」，高亮行 ${sel}`);

    let queries = 0;
    p.on('request', (q) => { if (q.url().includes('/selection/query')) queries += 1; });
    await p.click('button.chip-toggle:not([disabled])'); await sleep(1200);
    const chips = await p.$$eval('button.filter-chip', (xs) => xs.length);
    rec('点分面会重新查询', queries >= 1 && chips >= 1, `/selection/query ${queries} 次，筛选条 ${chips} 个`);

    await p.click('button.filter-chip.is-clear').catch(() => {}); await sleep(1000);
    await p.click('#runBatchBtn'); await sleep(800);
    const ok = await p.$('.modal .modal-foot button.btn-primary');
    if (ok) { await ok.click(); }
    await p.waitForFunction(() => location.hash.startsWith('#batch/'), null, { timeout: 15000 }).catch(() => {});
    // 跑多久看样品数（45 个样品要十几秒）；等到叠图出来为止
    await p.waitForSelector('#chartHost svg', { timeout: 60000 }).catch(() => {});
    const h = await p.evaluate(() => location.hash);
    const title = await p.textContent('#pageTitle');
    rec('直接跑对比并跳到对比页', h.startsWith('#batch/') && !!(await p.$('#chartHost svg')), `地址 ${h.slice(0, 30)}，标题「${title}」`);
    rec('处理页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // 4. 模型下拉：抽屉和 ⓘ 是同一份，在哪边换，另一边**立刻**跟上（不用关了重开）
  { const { ctx, p } = await fresh(b, 'batch/' + rid);
    await sleep(2000);
    await p.click('#aiToggle'); await sleep(800);
    const drawerSel = '#aiDrawer select.model-switch';
    await p.selectOption(drawerSel, { label: 'fake-2' }); await sleep(200);
    await p.click('button.info-dot'); await sleep(800);
    const popSel = '.info-pop select.model-switch';
    const popShows = await p.$eval(popSel, (s) => s.options[s.selectedIndex].textContent).catch(() => null);
    rec('抽屉换 → ⓘ 跟上', popShows === 'fake-2', `抽屉选 fake-2 后打开 ⓘ，ⓘ 显示「${popShows}」`);
    await p.selectOption(popSel, { label: 'fake-1' }); await sleep(200);
    const drawerShows = await p.$eval(drawerSel, (s) => s.options[s.selectedIndex].textContent);
    rec('ⓘ 换 → 抽屉立刻跟上', drawerShows === 'fake-1', `ⓘ 里选 fake-1，开着的抽屉显示「${drawerShows}」`);

    // ⓘ 里问一句：走的是共用的流式客户端，用的是刚选的模型
    const before = reqLog && fs.existsSync(reqLog) ? fs.readFileSync(reqLog, 'utf8').trim().split('\n').length : 0;
    await p.fill('textarea.info-input', '这个是什么意思'); await p.press('textarea.info-input', 'Enter');
    await sleep(4000);
    const answer = (await p.textContent('.info-thread')).trim();
    const lines = reqLog && fs.existsSync(reqLog) ? fs.readFileSync(reqLog, 'utf8').trim().split('\n') : [];
    const last = lines.length > before ? JSON.parse(lines[lines.length - 1]) : {};
    rec('ⓘ 流式回答', answer.length > 20 && last.model === 'fake-1', `回答 ${answer.length} 字，网关收到的模型 ${last.model}`);
    await p.keyboard.press('Escape'); await sleep(300);

    // 5. 弹窗开着时按 Esc：关弹窗，不连抽屉一起关
    await p.click('#chartHost button:has-text("导出脚本")'); await sleep(500);
    await p.keyboard.press('Escape'); await sleep(300);
    const st = await p.evaluate(() => ({
      modal: !!document.querySelector('.modal-backdrop'),
      drawer: document.querySelector('#aiToggle')?.getAttribute('aria-expanded'),
    }));
    rec('Esc 只关弹窗', !st.modal && st.drawer === 'true', `弹窗还在=${st.modal}，抽屉开着=${st.drawer}`);
    rec('AI 部分零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // 6. 图反复重画到同一个容器：ResizeObserver 不越挂越多
  { const count = () => {
      const R = window.ResizeObserver; window.__ro = { live: 0 };
      window.ResizeObserver = class extends R {
        observe(...a) { window.__ro.live += 1; return super.observe(...a); }
        disconnect() { window.__ro.live -= 1; return super.disconnect(); }
      };
    };
    const { ctx, p } = await fresh(b, 'batch/' + rid, count);
    await sleep(2500);
    const n0 = await p.evaluate(() => window.__ro.live);
    for (const mode of ['band', 'lines', 'band', 'lines', 'band']) {
      const s = await p.$('#chartHost select');
      if (!s) break;
      await s.selectOption({ index: mode === 'band' ? 1 : 0 }).catch(() => {}); await sleep(400);
    }
    const n1 = await p.evaluate(() => window.__ro.live);
    rec('重画不累积观察者', n1 <= n0 + 1, `重画 5 次前 ${n0} 个、后 ${n1} 个在观察`);
    await ctx.close(); }

  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
