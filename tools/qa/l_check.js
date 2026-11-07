// L 组（P12 里发现的零碎）的验收：
//   L-1 打开 AI 抽屉、点「新对话」不再在库里建空会话；第一次发送才建，连按两下只发一次
//   L-3 对比历史的行能用键盘打开
//
// 用法（准备工作见 README.md；第三个参数是这个工作区的 hte.db，用来数库里到底有几个会话 ——
// 历史接口现在不列空会话，光看接口看不出来；工作区里要至少跑过一次批处理）：
//   NODE_PATH=$(npm root -g) node tools/qa/l_check.js /path/to/workspace/hte.db
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');

const BASE = 'http://127.0.0.1:8799/';
const DB = process.argv[2];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`); };
const sql = (q) => execFileSync('python3', ['-c',
  'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute(sys.argv[2]).fetchone()[0])', DB, q]).toString().trim();
const nConv = () => Number(sql('SELECT COUNT(*) FROM conversation'));
const nEmpty = () => Number(sql('SELECT COUNT(*) FROM conversation c WHERE NOT EXISTS'
  + ' (SELECT 1 FROM message m WHERE m.conversation_id = c.conversation_id)'));

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  const menu = async () => {
    await p.click('.ai-head button[title="菜单"]'); await sleep(150);
    const items = await p.$$eval('button.ai-menu-item', (xs) => xs.map((x) => x.textContent));
    // 点抽屉正文关掉菜单（按 Esc 会把整个抽屉关了）
    const box = await p.locator('.ai-body').boundingBox();      // 右下角：离菜单和拖动条都远
    await p.mouse.click(box.x + box.width - 20, box.y + box.height - 20); await sleep(100);
    return items;
  };

  // 1. 开关抽屉、刷新页面、点「新对话」—— 库里一个会话都不多
  const before = nConv();
  const emptyBefore = nEmpty();     // 工作区里可能还有旧前端留下的（历史里已经不列了）
  const drawerOpen = () => p.evaluate(() => document.querySelector('#aiToggle')?.getAttribute('aria-expanded') === 'true');
  await p.goto(BASE + '#process', { waitUntil: 'networkidle' });
  for (let i = 0; i < 3; i++) {
    if (i) await p.reload({ waitUntil: 'networkidle' });   // 同一个地址再 goto 不会真的刷新
    await sleep(500);
    for (let k = 0; k < 3; k++) { await p.click('#aiToggle'); await sleep(300); }
    if (!(await drawerOpen())) { await p.click('#aiToggle'); await sleep(300); }
    await p.click('.ai-head button[title="菜单"]'); await sleep(150);
    await p.click('button.ai-menu-item:has-text("新对话")'); await sleep(400);
  }
  const after = nConv();
  rec('开抽屉不建会话', after === before, `刷新 3 次、开关抽屉 9 次以上、点「新对话」3 次：库里会话 ${before} → ${after}`);

  // 2. 还没说话时菜单里没有重命名 / 删除
  const empty = await menu();
  rec('没说话时菜单', !empty.includes('重命名') && !empty.includes('删除这个对话') && empty.includes('新对话'),
      `菜单：${empty.join(' / ')}`);

  // 3. 连按两下回车：只建一个会话、只发一句
  await p.fill('textarea.ai-input', '你好');
  await p.press('textarea.ai-input', 'Enter');
  await p.press('textarea.ai-input', 'Enter');
  await sleep(2500);
  const created = nConv() - after;
  const lastConv = sql('SELECT conversation_id FROM conversation ORDER BY rowid DESC LIMIT 1');
  const userMsgs = Number(sql(`SELECT COUNT(*) FROM message WHERE conversation_id='${lastConv}' AND role='user'`));
  rec('第一次发送才建、只发一次', created === 1 && userMsgs === 1, `新建会话 ${created} 个，这个会话里你的话 ${userMsgs} 句`);

  const saved = await menu();
  rec('说过话后菜单', saved.includes('重命名') && saved.includes('删除这个对话'), `菜单：${saved.join(' / ')}`);

  // 4. 历史里没有「0 条」
  await p.click('.ai-head button[title="菜单"]'); await sleep(150);
  await p.click('button.ai-menu-item:has-text("历史对话")'); await sleep(800);
  const rows = await p.$$eval('button.ai-histrow', (xs) => xs.map((x) => x.textContent));
  rec('历史里没有空会话', rows.length > 0 && !rows.some((t) => /(^|\D)0 条/.test(t)),   // 「10 条」不算
      `${rows.length} 行；第一行「${(rows[0] || '').slice(0, 30)}」`);
  rec('这一轮没多出空会话', nEmpty() === emptyBefore, `库里空会话 ${emptyBefore} → ${nEmpty()}`);
  // L-3 对比历史：Tab 走到第一行，回车打开
  await p.goto(BASE + '#history', { waitUntil: 'networkidle' }); await sleep(800);
  await p.focus('tr.is-clickable');
  const focused = await p.evaluate(() => document.activeElement?.matches('tr.is-clickable'));
  await p.keyboard.press('Enter'); await sleep(1500);
  const h = await p.evaluate(() => location.hash);
  rec('对比历史键盘打开', focused && h.startsWith('#batch/'), `行能聚焦=${focused}，回车后地址 ${h.slice(0, 24)}`);

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');

  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
