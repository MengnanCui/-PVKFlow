// D 组（结果审核 / 断链巡检 / 样品集管理）的验收。
//
// 准备：README.md 里的测试工作区；跑过 seed_relation.py（qa_x / qa_y）；
// 第一个参数的样品要跑过两次批处理（这样才有「作废了最新的，当前值退回上一次」可量）。
// 第二个参数是测试工作区的目录（断链那一条要把一个文件挪走再放回来）。
//   NODE_PATH=$(npm root -g) node tools/qa/d_check.js smp_xxx /tmp/hte-qa
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8799/';
const [sid, wsDir = '/tmp/hte-qa'] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(24)} ${detail}`); };

async function fresh(b, hash) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  p.errors = [];
  p.on('console', (m) => { if (m.type() === 'error') p.errors.push(m.text()); });
  p.on('pageerror', (e) => p.errors.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#' + hash, { waitUntil: 'networkidle' });
  return { ctx, p };
}
const get = (p, path) => p.evaluate(async (path) => (await fetch(path)).json(), path);
const post = (p, path, body) => p.evaluate(async ([path, body]) => (await fetch(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})).json(), [path, body]);

(async () => {
  const b = await chromium.launch();

  // ---------------------------------------------------------------- D-1 样品页审核
  { const { ctx, p } = await fresh(b, 'sample/' + sid);
    await sleep(2500);
    const before = await get(p, `/api/results/by-sample/${sid}`);
    const f = before.fields.find((x) => x.history.length >= 2 && x.current);
    const nRows = await p.$$eval('#body-results tbody tr:not(.history-row)', (xs) => xs.length);
    rec('样品页每个字段一行', nRows === before.fields.length, `表 ${nRows} 行，接口 ${before.fields.length} 个字段`);

    // 作废当前值：问理由 → 当前值退回上一次
    const row = p.locator(`#body-results tr[data-field="${f.field_name}"]`);
    await row.locator('.quality-ctl button[data-q="reject"]').click(); await sleep(300);
    const asked = await p.$eval('.modal h3', (x) => x.textContent).catch(() => '');
    await p.fill('.modal textarea', 'QA：样品裂了');
    await p.click('.modal button:has-text("确定")'); await sleep(1200);
    const after = await get(p, `/api/results/by-sample/${sid}`);
    const f2 = after.fields.find((x) => x.field_name === f.field_name);
    const prev = f.history.find((x) => x.id !== f.current.id && x.quality !== 'reject');
    const cellNow = await p.locator(`#body-results tr[data-field="${f.field_name}"] td.num`).textContent();
    const toasts = await p.$$eval('.toast', (xs) => xs.map((x) => x.textContent).join(' / '));
    rec('作废要问理由', asked.includes('作废'), `弹窗「${asked}」`);
    rec('作废后当前值退回上一次', f2.current?.id === prev?.id && f2.history.find((x) => x.id === f.current.id).quality_note === 'QA：样品裂了',
        `当前值 ${f.current.id} → ${f2.current?.id}（上一次 ${prev?.id}），理由已存`);
    rec('表上和提示都说了退回到哪', toasts.includes('当前值') && cellNow.length > 0, `提示「${toasts.slice(0, 60)}」`);

    // 历史展开：每一次都列出来，作废的那条划掉
    await p.locator(`#body-results tr[data-field="${f.field_name}"] button.linklike`).click(); await sleep(200);
    const hist = await p.$$eval(`#body-results tr[data-history-of="${f.field_name}"]`, (xs) => xs.map((x) => ({ hidden: x.hidden, rej: x.classList.contains('is-rejected') })));
    rec('历史展开、作废的划掉', hist.length === f.history.length && hist.every((x) => !x.hidden) && hist.filter((x) => x.rej).length === 1,
        `${hist.length} 行，划掉 ${hist.filter((x) => x.rej).length} 行`);

    // 改回来：在历史里把作废的那条标回「可信」
    await p.locator(`#body-results tr[data-history-of="${f.field_name}"].is-rejected .quality-ctl button[data-q="validated"]`).click(); await sleep(1200);
    const back = await get(p, `/api/results/by-sample/${sid}`);
    rec('改回来，当前值回到最新', back.fields.find((x) => x.field_name === f.field_name).current?.id === f.current.id, '作废 → 可信');

    // 最近一次处理整批作废 → 再整批可信
    await p.click('#body-results button:has-text("全部作废")'); await sleep(300);
    await p.click('.modal button:has-text("确定")'); await sleep(1200);
    const bulk = await get(p, `/api/results/by-sample/${sid}`);
    const latestRun = before.fields.flatMap((x) => x.history).reduce((a, c) => (c.id > a.id ? c : a)).analysis_run_id;
    const rejected = bulk.fields.flatMap((x) => x.history).filter((x) => x.analysis_run_id === latestRun);
    rec('最近一次处理整批作废', rejected.length > 0 && rejected.every((x) => x.quality === 'reject'), `${rejected.length} 条全部作废`);
    await p.click('#body-results button:has-text("全部可信")'); await sleep(1200);
    rec('样品页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // ---------------------------------------------------------------- D-1 构效关系页审核
  { const { ctx, p } = await fresh(b, 'relation');
    await sleep(1200);
    const R = '#page-relation';
    await p.locator(`${R} .figure-ctl select`).nth(0).selectOption('qa_x'); await sleep(800);
    await p.locator(`${R} .figure-ctl select`).nth(1).selectOption('qa_y'); await sleep(1200);
    const ref = await post(p, '/api/relation/pairs', { x: 'qa_x', y: 'qa_y', filter: {} });
    await p.click(`${R} details summary:has-text("逐个样品")`); await sleep(200);
    const victim = ref.points[0];
    const tr = p.locator(`${R} details[open] tbody tr`).first();
    await tr.locator('.quality-ctl').first().locator('button[data-q="reject"]').click(); await sleep(300);
    await p.click('.modal button:has-text("确定")'); await sleep(1800);
    const ref2 = await post(p, '/api/relation/pairs', { x: 'qa_x', y: 'qa_y', filter: {} });
    const dots = await p.evaluate(() => document.querySelector('#page-relation .figure svg g[stroke-width="1.2"]')?.childElementCount ?? -1);
    const stillOpen = await p.$$eval(`${R} details[open] tbody tr`, (xs) => xs.length);
    rec('作废 X 后这个点从图上拿掉', ref2.points.length === ref.points.length - 1 && dots === ref2.points.length
        && !ref2.points.some((q) => q.sample_id === victim.sample_id),
        `点 ${ref.points.length} → ${dots}（${victim.name.slice(0, 18)} 只有一次 qa_x）`);
    rec('刷新后表格还开着', stillOpen === ref2.points.length, `表 ${stillOpen} 行`);
    // 改回来（直接用接口 —— 这个样品已经不在表里了）
    await post(p, `/api/results/${victim.x_id}/quality`, { quality: 'review' });
    rec('构效关系页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // ---------------------------------------------------------------- D-2 断链巡检
  { const { ctx, p } = await fresh(b, 'storage');
    await sleep(1200);
    const files = await get(p, '/api/files?limit=5');
    const first = files.rows.find((x) => x.storage_mode === 'copied');
    const victim = await get(p, `/api/files/${first.artifact_id}`);     // 列表里没有 stored_path，详情里有
    const real = path.join(wsDir, victim.stored_path);
    const stash = real + '.qa-moved';
    fs.renameSync(real, stash);
    try {
      await p.click('button:has-text("检查一遍")'); await sleep(1500);
      const rep = await p.evaluate(() => {
        const sec = [...document.querySelectorAll('#page-storage .section')]
          .find((x) => x.querySelector('.section-title')?.textContent === '原始文件还在不在');
        return { text: sec?.textContent || '', rows: [...(sec?.querySelectorAll('table.missing-files tbody tr') || [])].map((r) => r.textContent) };
      });
      rec('断链列出是哪个文件', rep.text.includes('1 个找不到') && rep.rows.length === 1 && rep.rows[0].includes(victim.filename)
          && rep.rows[0].includes('新') && rep.rows[0].includes(victim.stored_path.split('/').pop()),
          `「${rep.text.match(/\d+ 个找不到[^。]*。/)?.[0] || rep.text.slice(0, 60)}」；表 ${rep.rows.length} 行`);
      // 总览上的提醒能直达这里
      await p.click('.nav-item[data-page="overview"]'); await sleep(1200);
      const note = await p.textContent('#page-overview .notice').catch(() => '');
      await p.click('#page-overview .notice button:has-text("去看是哪些")'); await sleep(1000);
      const h1 = await p.evaluate(() => location.hash);
      rec('总览的提醒能点过来', note.includes('1 个原始文件') && h1.includes('storage'), `「${note.slice(0, 30)}」→ ${h1}`);
    } finally { fs.renameSync(stash, real); }
    await p.click('button:has-text("检查一遍")'); await sleep(1500);
    const ok = await p.evaluate(() => [...document.querySelectorAll('#page-storage .section')]
      .find((x) => x.querySelector('.section-title')?.textContent === '原始文件还在不在')?.textContent || '');
    rec('放回去再查：标回正常', ok.includes('都在') && ok.includes('1 个之前找不到'), ok.match(/都在.*/)?.[0]?.slice(0, 60) || ok.slice(0, 60));
    rec('存储页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // ---------------------------------------------------------------- 自查：确认框点「确定」要返回 true
  // （原来永远返回 false —— 装模块前的确认、删 AI 对话、卸载模块从界面上点都没反应）
  { const { ctx, p } = await fresh(b, 'overview');
    await sleep(800);
    const ask = (how) => p.evaluate(async (how) => {
      const ui = await import('/assets/js/ui.js');
      const pr = ui.confirmDialog('QA：确定吗？', { confirmLabel: '好' });
      const btns = [...document.querySelectorAll('.modal .modal-foot button')];
      if (how === 'ok') btns.find((x) => x.textContent === '好').click();
      else if (how === 'cancel') btns.find((x) => x.textContent === '取消').click();
      else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      return pr;
    }, how);
    const r = [await ask('ok'), await ask('cancel'), await ask('esc')];
    rec('确认框：确定 / 取消 / Esc', r[0] === true && r[1] === false && r[2] === false, `返回 ${r.join(' / ')}`);
    await ctx.close(); }

  // ---------------------------------------------------------------- D-3 样品集管理
  { const { ctx, p } = await fresh(b, 'process');
    await sleep(1200);
    for (const st of (await get(p, '/api/selection/sets')).sets) {
      if (st.name.startsWith('QA')) await p.evaluate((id) => fetch(`/api/selection/sets/${id}`, { method: 'DELETE' }), st.set_id);
    }
    const nSamples = (await get(p, '/api/samples')).samples.length;
    const dyn = await post(p, '/api/selection/sets', { name: 'QA 动态', kind: 'dynamic', filter: { batch: ['ZG0013'] } });
    const pin = await post(p, '/api/selection/sets', { name: 'QA 固定', kind: 'pinned', filter: { batch: ['ZG0014'] } });
    await p.reload({ waitUntil: 'networkidle' }); await sleep(1200);
    await p.click('#page-process .facet-head button:has-text("管理")'); await sleep(400);
    const rowSel = (id) => `.modal .set-row[data-set-id="${id}"]`;
    const rows = await p.$$eval('.modal .set-row', (xs) => xs.length);
    rec('管理弹窗列出全部样品集', rows === (await get(p, '/api/selection/sets')).sets.length, `${rows} 行`);

    // 改成一个已有的名字：说出来，不改
    await p.fill(`${rowSel(dyn.set_id)} input`, 'QA 固定');
    const nErr = p.errors.length;
    await p.click(`${rowSel(dyn.set_id)} button:has-text("改名")`); await sleep(600);
    const t1 = await p.$$eval('.toast', (xs) => xs.map((x) => x.textContent).join(' / '));
    // 撞名那一下接口本来就该回 400，浏览器会记一条 —— 是故意的，不算报错
    p.errors = p.errors.slice(0, nErr).concat(p.errors.slice(nErr).filter((e) => !e.includes('400')));
    await p.fill(`${rowSel(dyn.set_id)} input`, 'QA 动态改');
    await p.press(`${rowSel(dyn.set_id)} input`, 'Enter'); await sleep(800);
    const afterRename = (await get(p, `/api/selection/sets/${dyn.set_id}`)).name;
    const chip = await p.$$eval('#page-process .facet-chips .chip-label', (xs) => xs.map((x) => x.textContent));
    rec('改名：撞名拦住、回车改好', t1.includes('已经有') && afterRename === 'QA 动态改' && chip.includes('QA 动态改'),
        `撞名提示「${t1.slice(0, 24)}」；现在叫「${afterRename}」，左栏跟着变=${chip.includes('QA 动态改')}`);

    // 钉死：确认框 → 变成固定集，个数就是此刻命中的
    await p.click(`${rowSel(dyn.set_id)} button:has-text("钉死")`); await sleep(300);
    const two = await p.$$eval('.modal', (xs) => xs.length);
    await p.click('.modal-backdrop:last-child button:has-text("钉死")'); await sleep(900);
    const frozen = await get(p, `/api/selection/sets/${dyn.set_id}`);
    rec('钉死成固定集', two === 2 && frozen.kind === 'pinned' && frozen.count === dyn.count,
        `确认框叠在上面=${two === 2}；kind=${frozen.kind}，${frozen.count} 个（原 ${dyn.count}）`);

    // 删除：确认框里按 Esc 只关确认框；再点一次确认，删掉，样品一个不少
    await p.click(`${rowSel(pin.set_id)} button:has-text("删除")`); await sleep(300);
    await p.keyboard.press('Escape'); await sleep(300);
    const left = await p.$$eval('.modal', (xs) => xs.length);
    const still = await get(p, `/api/selection/sets/${pin.set_id}`);
    rec('确认框里 Esc 只关一层', left === 1 && still.set_id === pin.set_id, `还开着 ${left} 个弹窗，样品集还在`);
    await p.click(`${rowSel(pin.set_id)} button:has-text("删除")`); await sleep(300);
    await p.click('.modal-backdrop:last-child button.btn-danger:has-text("删除")'); await sleep(900);
    // 用列表判断，不去 GET 那个已删的（会回 404，浏览器记成一条报错）
    const gone = (await get(p, '/api/selection/sets')).sets.some((x) => x.set_id === pin.set_id) ? 200 : 404;
    const n2 = (await get(p, '/api/samples')).samples.length;
    const rows2 = await p.$$eval('.modal .set-row', (xs) => xs.length);
    rec('删除：只删样品集', gone === 404 && n2 === nSamples && rows2 === rows - 1, `列表里${gone === 404 ? '没了' : '还在'}；样品 ${nSamples} → ${n2}；弹窗 ${rows} → ${rows2} 行`);
    await p.keyboard.press('Escape'); await sleep(200);
    await p.evaluate((id) => fetch(`/api/selection/sets/${id}`, { method: 'DELETE' }), dyn.set_id);
    rec('数据处理页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
