// C077 验收：下钻页回你来的地方、该点得进去的都点得进去、对比历史改名 / 删除、批处理一条路、地址栏换成 run。
//   NODE_PATH=$(npm root -g) node tools/qa/c077_check.js run_xxx（一次跑完的批处理，会被改名，不会被删）
// 删除那一条自己先跑一次小批处理再删它，不动你给的那次。
const { chromium } = require('playwright');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = 'http://127.0.0.1:8799/';
const out = [];
const rec = (name, ok, detail) => { out.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(20)} ${detail}`); };
const api = async (p, path, init) => p.evaluate(async ([u, i]) => {
  const r = await fetch(u, i ? { ...i, headers: { 'content-type': 'application/json' } } : undefined);
  return { status: r.status, body: await r.json().catch(() => null) };
}, [path, init || null]);

async function waitTask(p, id) {
  for (let i = 0; i < 240; i += 1) {
    const t = (await api(p, `/api/tasks/${id}`)).body;
    if (t?.done) return t;
    await sleep(500);
  }
  return null;
}

(async () => {
  const [run] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));
  p.on('console', (m) => { if (m.type() === 'error' && !/aborted|AbortError/i.test(m.text())) errs.push(m.text()); });
  const title = () => p.textContent('#pageTitle');
  const lit = () => p.$eval('.nav-item[aria-current="page"]', (x) => x.dataset.page).catch(() => null);

  // 1. 对比历史 → 批处理结果 → 样品行 → 样品页：返回一层层回去，侧栏一直亮「对比历史」
  await p.goto(BASE + '#history', { waitUntil: 'networkidle' }); await sleep(600);
  await p.click(`#page-history tr[data-run="${run}"] td:first-child`); await sleep(1500);
  const back1 = await p.textContent('#pageActions .back-btn');
  await p.click('#page-batch #tableHost details summary'); await sleep(200);     // 「全部 N 个样品」默认收着
  const row = p.locator('#page-batch #tableHost tr.is-clickable').first();
  const nRows = await p.locator('#page-batch #tableHost tr.is-clickable').count();
  await row.focus(); await p.keyboard.press('Enter'); await sleep(1500);
  const sampleHash = await p.evaluate(() => location.hash);
  const back2 = await p.textContent('#pageActions .back-btn');
  const lit2 = await lit();
  await p.click('#pageActions .back-btn'); await sleep(1200);
  const t3 = await title(); const back3 = await p.textContent('#pageActions .back-btn').catch(() => '');
  rec('下钻一层层返回', back1.includes('对比历史') && nRows > 0 && sampleHash.startsWith('#sample/')
      && back2.includes('批处理结果') && lit2 === 'history' && t3 !== '样品' && back3.includes('对比历史'),
      `对比页「${back1}」→ 样品行 ${nRows} 个、回车 ${sampleHash.slice(0, 22)}…「${back2}」侧栏亮 ${lit2} → 返回到「${t3}」「${back3}」`);

  // 2. 构效关系点进样品：返回回构效关系（原来回数据处理），侧栏亮构效关系
  await p.goto(BASE + '#relation', { waitUntil: 'networkidle' }); await sleep(1500);
  await p.click('#page-relation details summary:has-text("逐个样品")').catch(() => {}); await sleep(200);
  const relRow = p.locator('#page-relation details[open] table tbody button.linklike').first();
  let relOk = false; let relDetail = '构效关系页没有可点的样品';
  if (await relRow.count()) {
    await relRow.focus(); await p.keyboard.press('Enter'); await sleep(1200);
    const bk = await p.textContent('#pageActions .back-btn'); const l = await lit();
    await p.click('#pageActions .back-btn'); await sleep(800);
    relOk = bk.includes('构效关系') && l === 'relation' && (await title()) === '构效关系';
    relDetail = `样品页「${bk}」侧栏亮 ${l}，返回后「${await title()}」`;
  }
  rec('从构效关系进样品', relOk, relDetail);

  // 3. 直接打开样品链接（没有来处）：返回回数据处理
  // 只改地址栏的 # 不算重新打开（页面还在，来处是上一页）—— 要真的重新载入
  await p.goto(BASE + sampleHash); await p.reload({ waitUntil: 'networkidle' }); await sleep(800);
  const bkDirect = await p.textContent('#pageActions .back-btn');
  rec('直接打开时的返回', bkDirect.includes('数据处理'), `「${bkDirect}」`);

  // 4. 总览「最近的处理」点得进去
  await p.goto(BASE + '#overview', { waitUntil: 'networkidle' }); await sleep(800);
  const recent = p.locator('#page-overview tr.is-clickable').first();
  const nRecent = await p.locator('#page-overview tr.is-clickable').count();
  await recent.click(); await sleep(1200);
  const h4 = await p.evaluate(() => location.hash);
  rec('总览最近的处理', nRecent > 0 && /^#(batch|sample)\//.test(h4), `${nRecent} 行可点，点第一行到 ${h4.slice(0, 30)}`);

  // 5. 对比历史改名（改完改回去）
  await p.goto(BASE + '#history', { waitUntil: 'networkidle' }); await sleep(600);
  const oldName = (await p.textContent(`#page-history tr[data-run="${run}"] td:first-child .run-name`)).trim();
  await p.click(`#page-history tr[data-run="${run}"] button:has-text("改名")`); await sleep(300);
  await p.fill('.modal input', 'QA 改名测试'); await p.click('.modal button:has-text("保存")'); await sleep(900);
  const newName = (await p.textContent(`#page-history tr[data-run="${run}"] td:first-child .run-name`)).trim();
  await api(p, `/api/batch/runs/${run}`, { method: 'PATCH', body: JSON.stringify({ title: oldName }) });
  rec('对比历史改名', newName === 'QA 改名测试', `「${oldName}」→「${newName}」（已改回）`);

  // 6. 跑一次小批处理 → 地址栏换成 run_…；然后在对比历史里删掉它
  const ids = (await api(p, '/api/selection/ids', { method: 'POST', body: JSON.stringify({ filter: { has_matrix: true }, limit: 2 }) })).body.sample_ids;
  const sub = (await api(p, '/api/batch/run', { method: 'POST', body: JSON.stringify({ filter: { ids }, recipe: {}, title: 'QA 待删' }) })).body;
  await p.goto(BASE + `#batch/${sub.task.task_id}`); await sleep(300);
  const t = await waitTask(p, sub.task.task_id); await sleep(1500);
  const h6 = await p.evaluate(() => location.hash);
  const newRun = t?.result?.parent_run_id;
  rec('跑完地址栏换成 run', h6 === `#batch/${newRun}`, `任务 ${sub.task.task_id} → ${h6}`);

  await p.goto(BASE + '#history', { waitUntil: 'networkidle' }); await sleep(600);
  await p.click(`#page-history tr[data-run="${newRun}"] button:has-text("删除")`); await sleep(800);
  const msg = await p.textContent('.modal');
  await p.click('.modal button:has-text("删除")'); await sleep(1200);
  const still = await p.locator(`#page-history tr[data-run="${newRun}"]`).count();
  const nErr = errs.length;
  const gone = (await api(p, `/api/batch/runs/${newRun}`)).status;    // 这一下 404 是我们故意问的，不算报错
  errs.length = nErr;
  rec('对比历史删除', /2 个样品/.test(msg) && /关键结果/.test(msg) && still === 0 && gone === 404,
      `确认框「${msg.replace(/\s+/g, ' ').slice(0, 90)}…」；删后列表里 ${still} 行、接口 ${gone}`);

  rec('零报错', errs.length === 0, errs.slice(0, 3).join(' | ') || '无');
  await b.close();
  console.log(`\n${out.filter(Boolean).length} / ${out.length} 通过`);
})();
