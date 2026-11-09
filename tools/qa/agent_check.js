// 智能体页的验收。
//
// 准备：README 里的测试工作区 + 假网关（tools/qa/fake_gateway.py 认得智能体的剧本）；
// 设置里配三个模型 fake-1、fake-2、fake-notools（最后一个专门拒收 tools 参数，逼平台退到动作协议）。
//   NODE_PATH=$(npm root -g) node tools/qa/agent_check.js
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(24)} ${detail}`); };

const get = (p, path) => p.evaluate(async (path) => (await fetch(path)).json(), path);
const post = (p, path, body) => p.evaluate(async ([path, body]) => (await fetch(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})).json(), [path, body]);

const L = '#page-agent .agent-log';
async function ask(p, text, wait = 2500) {
  await p.fill('#page-agent textarea', text);
  await p.keyboard.press('Enter');
  await sleep(wait);
}
const lastDone = (p) => p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-done`)].pop()?.textContent || '', L);
const quality = async (p, sid, field) => (await get(p, `/api/results/by-sample/${sid}`)).fields.find((f) => f.field_name === field);

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#agent', { waitUntil: 'networkidle' });
  await sleep(800);
  await p.evaluate(() => localStorage.setItem('hte.ai.model', JSON.stringify({ provider: '假网关', model: 'fake-1' })));
  await post(p, '/api/agent/settings', { permission: 'ask' });
  await p.reload({ waitUntil: 'networkidle' }); await sleep(800);

  // 1. 只读：一轮里两个工具，结果回给模型，回答里的数和接口一致
  const stopShown = await p.$eval('#page-agent button:has-text("停止")', (x) => getComputedStyle(x).display !== 'none').catch(() => null);
  await ask(p, '现在平台里有什么数据？');
  const counts = (await get(p, '/api/overview')).counts;
  const cards = await p.$$eval(`${L} details.agent-tool`, (xs) => xs.map((x) => ({ name: x.dataset.name, ok: x.classList.contains('is-ok') })));
  const said = await p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-msg.is-bot`)].pop()?.textContent || '', L);
  rec('只读工具直接跑', cards.length === 2 && cards.every((c) => c.ok) && cards.map((c) => c.name).join() === 'overview,list_fields',
      `${cards.map((c) => `${c.name}${c.ok ? '✓' : '✗'}`).join('、')}；空闲时「停止」${stopShown ? '露出来了' : '藏着'}`);
  rec('回答用的是读到的数', said.includes(`${counts.samples} 个样品`) && !stopShown, `「${said}」；接口 ${counts.samples} 个样品`);
  await p.locator(`${L} details.agent-tool`).first().locator('summary').click(); await sleep(150);
  const body = await p.locator(`${L} details.agent-tool`).first().textContent();
  rec('工具卡展开看得到参数和原文', body.includes('参数') && body.includes('"counts"'), body.replace(/\s+/g, ' ').slice(0, 60));

  // 2. 写：停下来问；没批之前库里不动；批了才改
  const samples = (await get(p, '/api/samples')).samples;
  const s0 = samples.find((s) => s.n_results);
  const f0 = await quality(p, s0.sample_id, 'qa_x');
  await ask(p, `作废结果 ${f0.current.id}`);
  const card = p.locator(`${L} .agent-approval.is-open`).last();
  const prev = await card.textContent().catch(() => '');
  const mid = await quality(p, s0.sample_id, 'qa_x');
  rec('会改数据的先停下来问', prev.includes('要执行「审核结果」吗') && prev.includes('作废') && prev.includes(s0.name.slice(0, 12))
      && mid.current.id === f0.current.id && mid.current.quality === f0.current.quality,
      `审批卡「${prev.replace(/\s+/g, ' ').slice(0, 50)}」；库里还是「${mid.current.quality}」`);
  await card.locator('button:has-text("批准")').click(); await sleep(2500);
  const after = await quality(p, s0.sample_id, 'qa_x');
  const rejected = after.history.find((x) => x.id === f0.current.id);
  const settled = await p.locator(`${L} .agent-approval`).last().textContent();
  rec('批了才执行', rejected.quality === 'reject' && rejected.quality_note === 'QA 智能体' && settled.includes('已批准')
      && (await lastDone(p)).includes('轮'), `结果 ${f0.current.id} → ${rejected.quality}；卡上「已批准」`);
  await post(p, `/api/results/${f0.current.id}/quality`, { quality: 'review' });

  // 3. 拒绝：告诉模型，库里不动
  await ask(p, `作废结果 ${f0.current.id}`);
  await p.locator(`${L} .agent-approval.is-open`).last().locator('button:has-text("拒绝")').click(); await sleep(2500);
  const r3 = await quality(p, s0.sample_id, 'qa_x');
  const said3 = await p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-msg.is-bot`)].pop()?.textContent || '', L);
  rec('拒绝：不执行、模型知道', r3.history.find((x) => x.id === f0.current.id).quality === 'review' && said3.includes('没动'),
      `库里还是 review；模型说「${said3}」`);

  // 4. 「本次会话内不再问」：下一次同类操作直接做
  const s1 = samples.filter((s) => s.n_results)[1];
  const f1 = await quality(p, s1.sample_id, 'qa_x');
  await ask(p, `作废结果 ${f0.current.id}`);
  const c4 = p.locator(`${L} .agent-approval.is-open`).last();
  await c4.locator('input[type=checkbox]').check();
  await c4.locator('button:has-text("批准")').click(); await sleep(2500);
  const nApprovals = await p.$$eval(`${L} .agent-approval`, (xs) => xs.length);
  await ask(p, `作废结果 ${f1.current.id}`);
  const nApprovals2 = await p.$$eval(`${L} .agent-approval`, (xs) => xs.length);
  const r4 = (await quality(p, s1.sample_id, 'qa_x')).history.find((x) => x.id === f1.current.id);
  const note4 = await p.evaluate((L) => [...document.querySelectorAll(`${L} details.agent-tool`)].pop()?.querySelector('summary')?.textContent || '', L);
  rec('记住之后同类不再问', nApprovals2 === nApprovals && r4.quality === 'reject' && note4.includes('不再问'),
      `审批卡 ${nApprovals} → ${nApprovals2}；结果 ${f1.current.id} → ${r4.quality}`);
  await post(p, '/api/results/quality', { ids: [f0.current.id, f1.current.id], quality: 'review' });

  // 5. 删除：每次都问，没有「记住」可勾
  const st = await post(p, '/api/selection/sets', { name: `QA 智能体删 ${Date.now() % 100000}`, kind: 'pinned', sample_ids: [s0.sample_id] });
  await ask(p, `删除样品集 ${st.set_id}`);
  const c5 = p.locator(`${L} .agent-approval.is-open`).last();
  const t5 = await c5.textContent();
  const hasBox = await c5.locator('input[type=checkbox]').count();
  rec('删除每次都问、记不住', t5.includes('删除') && t5.includes(st.name) && t5.includes('记不住') && hasBox === 0, t5.replace(/\s+/g, ' ').slice(0, 60));
  await c5.locator('button:has-text("批准")').click(); await sleep(2500);
  const gone = !(await get(p, '/api/selection/sets')).sets.some((x) => x.set_id === st.set_id);
  rec('批了才删', gone, `样品集${gone ? '已删' : '还在'}`);

  // 6. 停在审批上时刷新页面：从会话列表打开，还能批（新开一个会话 —— 上一个里「审核结果」已经勾了不再问）
  await p.click('#page-agent button[title^="新会话"]'); await sleep(300);
  await ask(p, '现在平台里有什么数据？');
  await ask(p, `作废结果 ${f0.current.id}`);
  const sid = (await get(p, '/api/agent/sessions')).sessions[0].session_id;
  await p.reload({ waitUntil: 'networkidle' }); await sleep(1000);
  // 轻量窗口（P-53）：会话在顶栏的下拉里，停在审批上的标 ⏸
  const waitingTag = await p.$eval(`#page-agent .agent-session-sel option[value="${sid}"]`, (x) => x.textContent);
  await p.selectOption('#page-agent .agent-session-sel', sid); await sleep(800);
  const replay = await p.evaluate((L) => ({
    cards: document.querySelectorAll(`${L} details.agent-tool`).length,
    open: document.querySelectorAll(`${L} .agent-approval.is-open`).length,
    closed: document.querySelectorAll(`${L} .agent-approval:not(.is-open)`).length,
  }), L);
  rec('刷新后会话原样回放', waitingTag.includes('⏸') && replay.cards === 3 && replay.open === 1 && replay.closed === 0,
      `下拉里${waitingTag.includes('⏸') ? '标着' : '没标'}「⏸」；工具卡 ${replay.cards}、待批 ${replay.open}、已处理 ${replay.closed}`);
  await p.locator(`${L} .agent-approval.is-open`).last().locator('button:has-text("拒绝")').click(); await sleep(2500);
  const s6 = await get(p, `/api/agent/sessions/${sid}`);
  const q6 = (await quality(p, s0.sample_id, 'qa_x')).history.find((x) => x.id === f0.current.id).quality;
  rec('刷新后照样能批', s6.pending.length === 0 && q6 === 'review' && !s6.events.slice(-3).some((e) => e.type === 'error'),
      `pending ${s6.pending.length}；拒绝后结果仍是 ${q6}`);

  // 7. 轮数上限：如实说停在哪
  await p.click('#page-agent button[title^="新会话"]'); await sleep(300);
  await ask(p, '一直查', 9000);
  rec('撞上轮数上限如实说', (await lastDone(p)).includes('轮数上限'), `「${await lastDone(p)}」`);

  // 8. 停止
  await p.click('#page-agent button[title^="新会话"]'); await sleep(300);
  await ask(p, '一直查', 600);
  await p.click('#page-agent button:has-text("停止")'); await sleep(1500);
  const sid8 = (await get(p, '/api/agent/sessions')).sessions[0].session_id;
  const s8 = await get(p, `/api/agent/sessions/${sid8}`);
  rec('停止：界面和库里都停了', (await lastDone(p)).includes('已停止') && s8.events.at(-1).reason === 'canceled',
      `界面「${await lastDone(p)}」；库里最后一条 ${s8.events.at(-1).type}/${s8.events.at(-1).reason}`);

  // 9. 网关不认 tools：自动退到动作协议，照样能查
  await p.click('#page-agent button[title^="新会话"]'); await sleep(300);
  await p.selectOption('#page-agent select.model-switch', { label: 'fake-notools' }); await sleep(200);
  await ask(p, '现在平台里有什么数据？', 3000);
  const said9 = await p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-msg.is-bot`)].pop()?.textContent || '', L);
  rec('网关不认 tools 时退到动作协议', said9.includes('个样品') && (await lastDone(p)).includes('动作协议'), `「${said9}」·「${await lastDone(p)}」`);
  await p.selectOption('#page-agent select.model-switch', { label: 'fake-1' });

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
