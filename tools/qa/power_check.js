// 智能体：权限整理（P-51）、跑命令（P-52）、询问 / 自动 + 计划 / 直接执行 + 轻量窗口（P-53）的验收。
//
// 准备：README 里的测试工作区 + 假网关（认「跑命令 …」「写文件 …」，以及计划模式）。参数是工作区目录。
//   NODE_PATH=$(npm root -g) node tools/qa/power_check.js /tmp/hte-qa
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8799/';
const [ws = '/tmp/hte-qa'] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(24)} ${detail}`); };
const post = (p, u, body) => p.evaluate(async ([u, body]) => (await fetch(u, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})).json(), [u, body]);

const L = '#page-agent .agent-log';
async function say(p, text, wait = 2500) {
  await p.fill('#page-agent textarea', text);
  await p.keyboard.press('Enter');
  await sleep(wait);
}
const lastBot = (p) => p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-msg.is-bot`)].pop()?.textContent || '', L);
const checked = (p, group) => p.$eval(`#page-agent .seg[aria-label="${group}"] [aria-checked="true"]`, (x) => x.textContent);

(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#agent', { waitUntil: 'networkidle' });
  await p.evaluate(() => { localStorage.setItem('hte.ai.model', JSON.stringify({ provider: '假网关', model: 'fake-1' }));
                           localStorage.setItem('hte.agent.runMode', 'exec'); });
  await post(p, '/api/agent/settings', { permission: 'ask', commands: true, roots: [] });
  await p.reload({ waitUntil: 'networkidle' }); await sleep(1000);

  // 1. 轻量窗口：一栏、顶栏有权限 / 模式开关，设置收在 ⚙ 里
  const shape = await p.evaluate(() => ({
    bar: !!document.querySelector('#page-agent .agent-bar'),
    side: !!document.querySelector('#page-agent .agent-side'),
    drawerHidden: document.querySelector('#page-agent .agent-drawer')?.hidden,
  }));
  rec('一栏的轻量窗口', shape.bar && !shape.side && shape.drawerHidden
      && (await checked(p, '权限')) === '询问' && (await checked(p, '模式')) === '直接执行', JSON.stringify(shape));

  // 2. 斜杠命令切模式、切权限（存到服务端）
  await say(p, '/auto', 400);
  await say(p, '/plan', 400);
  const st = await p.evaluate(async () => (await fetch('/api/agent/settings')).json());
  rec('斜杠命令', (await checked(p, '权限')) === '自动' && (await checked(p, '模式')) === '先出计划' && st.permission === 'auto',
      `权限 ${await checked(p, '权限')} / 模式 ${await checked(p, '模式')}；服务端 permission=${st.permission}`);
  await say(p, '/ask', 400);

  // 3. 计划模式：只调研、交计划；按计划执行
  const plotPy = path.join(ws, 'agent', 'plot.py');
  await say(p, '帮我画各批次的膜厚', 3000);
  const plan = await p.locator(`${L} .agent-plan`).last().textContent().catch(() => '');
  const tools = await p.$$eval(`${L} details.agent-tool`, (xs) => xs.map((x) => x.dataset.name));
  rec('先出计划：只调研、交计划', plan.includes('计划：画各批次的膜厚') && tools.join() === 'list_fields,propose_plan'
      && (await p.locator(`${L} .agent-approval`).count()) === 0, `工具 ${tools.join(' → ')}`);
  await p.locator(`${L} .agent-plan button:has-text("按计划执行")`).last().click(); await sleep(2500);
  const userTags = await p.$$eval(`${L} .agent-msg.is-user`, (xs) => xs.map((x) => x.textContent));
  rec('按计划执行', userTags.at(-1).includes('按计划执行') && (await lastBot(p)).includes('按计划做完了'), `「${await lastBot(p)}」`);
  await say(p, '/exec', 400);

  // 4. 跑命令（询问）：先问，审批卡写明工作目录；批了在专用目录里跑
  await say(p, '/new', 300);
  await say(p, '跑命令 python -c "import os; print(os.getcwd())"');
  const ap = await p.locator(`${L} .agent-approval.is-open`).last().textContent().catch(() => '');
  await p.locator(`${L} .agent-approval.is-open button:has-text("批准")`).last().click(); await sleep(2500);
  rec('跑命令先问、在专用目录里跑', ap.includes('在专用目录 agent/ 里跑') && (await lastBot(p)).includes('退出码 0')
      && (await lastBot(p)).includes(path.join(path.basename(ws), 'agent')), `「${(await lastBot(p)).slice(0, 70)}」`);

  // 5. 自动：普通命令直接跑；危险命令仍然问
  await say(p, '/auto', 400);
  await say(p, '跑命令 echo 自动跑了');
  const autoOk = (await lastBot(p)).includes('自动跑了') && (await p.locator(`${L} .agent-approval.is-open`).count()) === 0;
  await say(p, '跑命令 rm -rf old_stuff');
  const danger = await p.locator(`${L} .agent-approval.is-open`).last().textContent().catch(() => '');
  rec('自动模式：危险命令仍然问', autoOk && danger.includes('每次都要你点头') && !danger.includes('不再问'),
      `普通命令自动跑了=${autoOk}；危险命令「${danger.replace(/\s+/g, ' ').slice(0, 50)}」`);
  await p.locator(`${L} .agent-approval.is-open button:has-text("拒绝")`).last().click(); await sleep(1500);
  await say(p, '/ask', 400);

  // 6. 写文件只能写在专用目录；写平台核心被拒；/undo 撤销这个会话的文件改动
  await say(p, '写文件 agent/power.md');
  await p.locator(`${L} .agent-approval.is-open button:has-text("批准")`).last().click(); await sleep(2000);
  const made = fs.existsSync(path.join(ws, 'agent', 'power.md'));
  await say(p, '写文件 notes.md');
  const denied = await p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-approval`)].pop()?.textContent || '', L);
  if (await p.locator(`${L} .agent-approval.is-open`).count()) {
    await p.locator(`${L} .agent-approval.is-open button:has-text("批准")`).last().click(); await sleep(2000);
  }
  const outside = await p.evaluate((L) => [...document.querySelectorAll(`${L} details.agent-tool`)].pop()?.textContent || '', L);
  rec('只能写在专用目录', made && !fs.existsSync(path.join(ws, 'notes.md')) && outside.includes('只读') && outside.includes('agent/'),
      `agent/power.md ${made ? '写了' : '没写'}；工作区根目录写被拒「${(outside.match(/工作区是只读的[^。"]*/) || [''])[0]}」`);
  p.once('dialog', (d) => d.accept());
  await p.fill('#page-agent textarea', '/undo'); await p.keyboard.press('Enter'); await sleep(400);
  await p.locator('.modal button:has-text("撤销")').click(); await sleep(1200);
  const undone = await p.evaluate((L) => [...document.querySelectorAll(`${L} .agent-done`)].pop()?.textContent || '', L);
  rec('/undo 撤销这个会话的文件', !fs.existsSync(path.join(ws, 'agent', 'power.md')) && undone.includes('已撤销'), `「${undone}」`);

  // 7. ⚙ 抽屉：权限一览；关掉跑命令之后它就拿不到这个工具
  await p.click('#page-agent button[aria-label="设置"]'); await sleep(200);
  const drawer = await p.textContent('#page-agent .agent-drawer');
  await p.locator('#page-agent .agent-drawer label:has-text("允许跑命令") input').uncheck(); await sleep(600);
  const st2 = await p.evaluate(async () => (await fetch('/api/agent/settings')).json());
  rec('⚙ 权限一览、能关命令', drawer.includes('任何时候都不能写') && drawer.includes('专用目录') && st2.commands === false
      && (await p.textContent('#page-agent .agent-foot')).includes('跑命令：关'), `commands=${st2.commands}`);
  await post(p, '/api/agent/settings', { commands: true });
  if (fs.existsSync(plotPy)) fs.unlinkSync(plotPy);

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
