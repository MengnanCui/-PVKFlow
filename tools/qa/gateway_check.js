// 网关（照 IFlow 改）的验收：设置页的协议 / 思考 / 拉取并测速，以及走 Anthropic 协议的 AI 助手。
//
// 准备：README 里的测试工作区 + 假网关（tools/qa/fake_gateway.py 认 /v1/messages）；设置里已经有「假网关」那个 provider。
//   NODE_PATH=$(npm root -g) node tools/qa/gateway_check.js
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799/';
const GW = 'http://127.0.0.1:8797';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(24)} ${detail}`); };
const get = (p, path) => p.evaluate(async (path) => (await fetch(path)).json(), path);

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#settings', { waitUntil: 'networkidle' });
  await sleep(1200);

  // 1. 拉取并测速：同家族只测最新的；每个测过的有延迟；能停
  const form = p.locator('#page-settings .field:has(label:has-text("接口地址"))');
  await form.locator('input').first().fill(GW + '/v1');
  await p.click('#page-settings button:has-text("拉取并测速")');
  await sleep(2500);
  const grid = await p.$$eval('#page-settings .model-grid .model-opt', (xs) => xs.map((x) => x.textContent));
  const note = await p.textContent('#page-settings .model-pick');
  const timed = grid.filter((t) => / ms/.test(t));
  rec('拉取并测速', grid.length === 4 && timed.length === 3 && note.includes('旧版本没测') && !grid.find((t) => t.startsWith('fake-1'))?.includes(' ms'),
      `${grid.length} 个；测了 ${timed.length} 个（${timed.map((t) => t.replace(/\s+/g, ' ')).join(' / ')}）；fake-1 被 fake-2 顶掉`);

  // 2. 存一个 Anthropic 协议的 provider：预设会顺手填协议；协议和思考都存下来
  await p.selectOption('#page-settings select[aria-label="协议"]', 'anthropic-messages');
  await p.selectOption('#page-settings select[aria-label="思考"]', 'on');
  await p.locator('#page-settings .field:has(label:has-text("密钥"))').locator('input').fill('ak-qa-anthropic');
  await p.locator('#page-settings .field:has(label:has-text("名字"))').locator('input').fill('假 Anthropic');
  // 表单默认勾着第一个 provider 已存的模型 —— 这个新 provider 只要 claude-fake
  const checked = p.locator('#page-settings .model-pick input[type=checkbox]:checked');
  while (await checked.count()) await checked.first().uncheck();
  const manual = p.locator('#page-settings .field:has(label:has-text("模型")) > div.mt-3 input');
  await manual.fill('claude-fake');
  await p.click('#page-settings button:has-text("保存并启用")');
  await sleep(1500);
  const cfg = await get(p, '/api/settings/models');
  const ap = cfg.providers.find((x) => x.name === '假 Anthropic');
  rec('Anthropic 协议存得下', ap && ap.api === 'anthropic-messages' && ap.thinking === 'on' && ap.models[0].id === 'claude-fake'
      && !JSON.stringify(cfg).includes('ak-qa-anthropic'), ap ? `api=${ap.api} thinking=${ap.thinking} 模型=${ap.models.map((m) => m.id)}` : '没存上');
  await p.reload({ waitUntil: 'networkidle' }); await sleep(1000);
  const row = await p.$$eval('#page-settings table.data tbody tr', (xs) => xs.map((x) => x.textContent).find((t) => t.includes('假 Anthropic')) || '');
  rec('配置表上看得到协议和思考', row.includes('Anthropic') && row.includes('思考开'), row.replace(/\s+/g, ' ').slice(0, 80));

  // 3. AI 助手走 Anthropic 协议：x-api-key、system 单列、流式事件都对
  await p.evaluate(() => localStorage.setItem('hte.ai.model', JSON.stringify({ provider: '假 Anthropic', model: 'claude-fake' })));
  await p.goto(BASE + '#overview', { waitUntil: 'networkidle' }); await sleep(800);
  await p.click('#aiToggle'); await sleep(600);
  await p.fill('textarea.ai-input', '你好'); await p.press('textarea.ai-input', 'Enter');
  await sleep(2500);
  const body = await p.textContent('#aiDrawer .ai-body');
  rec('AI 助手走 Anthropic 协议', body.includes('Anthropic 协议收到了（system 单列=True）'), body.replace(/\s+/g, ' ').slice(-60));
  await p.evaluate(() => localStorage.setItem('hte.ai.model', JSON.stringify({ provider: '假网关', model: 'fake-1' })));

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
