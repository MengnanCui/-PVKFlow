// 智能体的文件 / 批处理 / 任务（P-49）的验收。
//
// 准备：README 里的测试工作区 + 假网关（认「写文件 / 读文件 / 跑批处理」这几句）；样品里要有批次 ZG0013。
// 第一个参数是测试工作区目录（会在里面建 agent/ 下的文件，最后撤销掉）。
//   NODE_PATH=$(npm root -g) node tools/qa/pi_check.js /tmp/hte-qa
const { chromium } = require('playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = 'http://127.0.0.1:8799/';
const [ws = '/tmp/hte-qa'] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(24)} ${detail}`); };
const get = (p, u) => p.evaluate(async (u) => (await fetch(u)).json(), u);
const post = (p, u, body) => p.evaluate(async ([u, body]) => (await fetch(u, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})).json(), [u, body]);

const L = '#page-agent .agent-log';
async function ask(p, text, wait = 2500) {
  await p.fill('#page-agent textarea', text);
  await p.keyboard.press('Enter');
  await sleep(wait);
}
const lastBot = (p) => p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-msg.is-bot`)].pop()?.textContent || '', L);

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#agent', { waitUntil: 'networkidle' });
  await p.evaluate(() => localStorage.setItem('hte.ai.model', JSON.stringify({ provider: '假网关', model: 'fake-1' })));
  await post(p, '/api/agent/settings', { permission: 'ask', roots: [] });
  await p.reload({ waitUntil: 'networkidle' }); await sleep(1000);
  await p.click('#page-agent button[title^="新会话"]'); await sleep(200);

  // 1. 写文件：先停下来问，审批卡上有差异；没批之前磁盘上没有；批了才写；卡上能撤销
  const target = path.join(ws, 'agent', 'QA报告.md');
  if (fs.existsSync(target)) fs.unlinkSync(target);
  await ask(p, '写文件 agent/QA报告.md');
  const card = p.locator(`${L} .agent-approval.is-open`).last();
  const pv = await card.textContent().catch(() => '');
  const adds = await card.locator('.diff-add').count();
  rec('写文件先问、带差异', pv.includes('写文件') && pv.includes('新建文件 agent') && adds >= 2 && !fs.existsSync(target),
      `审批卡「${pv.replace(/\s+/g, ' ').slice(0, 40)}」，绿色的加行 ${adds} 行；磁盘上${fs.existsSync(target) ? '已经有了' : '还没有'}`);
  await card.locator('button:has-text("批准")').click(); await sleep(2500);
  const written = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : '';
  rec('批了才写', written.includes('膜厚偏薄') && (await lastBot(p)).includes('文件写好了'), `文件 ${written.length} 字；「${await lastBot(p)}」`);

  // 2. 读文件（读回刚写的）
  await ask(p, '读文件 agent/QA报告.md');
  const rd = await p.evaluate((L) => [...document.querySelectorAll(`${L} details.agent-tool`)].pop()?.dataset.name, L);
  rec('读文件直接跑', rd === 'read_file' && (await lastBot(p)).includes('一共 3 行'), `「${await lastBot(p)}」`);

  // 3. 卡片上「撤销这次改动」：文件没了（新建的撤销 = 删掉）
  const undo = p.locator(`${L} details.agent-tool[data-name="write_file"] summary button`).first();
  await undo.click(); await sleep(800);
  const undoText = await undo.textContent();
  rec('卡片上撤销', !fs.existsSync(target) && undoText === '已撤销', `文件${fs.existsSync(target) ? '还在' : '没了'}；按钮「${undoText}」`);

  // 4. 名单外的路径：拒绝；加文件夹（只读）之后读得到、写不进
  const extra = fs.mkdtempSync(path.join(os.tmpdir(), 'qa-root-'));
  fs.writeFileSync(path.join(extra, 'note.txt'), '外面的文件\n第二行\n第三行\n第四行\n');
  await ask(p, `读文件 ${path.join(extra, 'note.txt')}`);
  const deny = await p.evaluate((L) => [...document.querySelectorAll(`${L} details.agent-tool`)].pop()?.textContent || '', L);
  await p.click('#page-agent button[aria-label="设置"]'); await sleep(200);      // 文件夹在 ⚙ 抽屉里
  await p.fill('#page-agent .agent-drawer input[aria-label="要加的文件夹"]', extra);
  await p.click('#page-agent .agent-drawer button:has-text("加上")'); await sleep(800);
  const rows = await p.$$eval('#page-agent .agent-root-row', (xs) => xs.map((x) => x.textContent));
  await ask(p, `读文件 ${path.join(extra, 'note.txt')}`);
  rec('名单外拒绝、加上就能读', deny.includes('不在能碰的文件夹里') && rows.some((t) => t.includes(path.basename(extra)) && t.includes('只读'))
      && (await lastBot(p)).includes('一共 4 行'), `加上后：「${await lastBot(p)}」；名单 ${rows.length} 项`);
  await post(p, '/api/agent/settings', { roots: [] });
  await p.click('#page-agent button[aria-label="设置"]'); await sleep(200);

  // 5. 跑批处理：先问（说清楚几个样品、配方）；批了 → 等任务 → 汇总
  const nZ = (await get(p, '/api/samples')).samples.filter((s) => s.batch === 'ZG0013').length;
  const before = (await get(p, '/api/batch/runs?limit=100')).runs.length;
  await ask(p, '跑批处理 ZG0013');
  const bc = p.locator(`${L} .agent-approval.is-open`).last();
  const bpv = await bc.textContent().catch(() => '');
  rec('跑批处理先问', bpv.includes(`对 ${nZ} 个样品跑一次批处理（批次 ZG0013）`) && bpv.includes('配方：默认'), bpv.replace(/\s+/g, ' ').slice(0, 70));
  await bc.locator('button:has-text("批准")').click();
  await sleep(12000);
  const runs = await get(p, '/api/batch/runs?limit=100');  // 默认只给 20 条：工作区攒够 20 次之后「多了一次」就数不出来
  const list = runs.runs || runs.rows;
  const names = await p.$$eval(`${L} details.agent-tool`, (xs) => xs.slice(-3).map((x) => x.dataset.name));
  rec('批处理跑完并汇总', list.length === before + 1 && names.join() === 'run_batch,get_task,batch_summary'
      && (await lastBot(p)).includes(`${nZ} 个样品成功`), `工具 ${names.join(' → ')}；「${await lastBot(p)}」`);

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
