// P-44 验收：在 AI 抽屉里问一句，看模型（假网关）实际收到的 system 里有没有曲线摘要。
// 也点一次样品页上的 ⓘ 提问 —— 那条路的范围是这一个样品，应该也带上它的曲线。
// 参数：样品 id、假网关记录文件（tools/qa/fake_gateway.py 的第二个参数）
const { chromium } = require('playwright');
const fs = require('fs');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 模型收到的全部文字。事实包在一条 user 消息里（「已知事实：…」），不在 system 里
const lastSystem = (log) => {
  const lines = fs.readFileSync(log, 'utf8').trim().split('\n');
  const req = JSON.parse(lines[lines.length - 1]);
  return (req.messages || []).map((m) => (typeof m.content === 'string' ? m.content
    : JSON.stringify(m.content))).join('\n');
};

(async () => {
  const [sid, log] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));

  // 1. AI 抽屉：全部命中的 3 个样品
  await p.goto('http://127.0.0.1:8799/#process', { waitUntil: 'networkidle' });
  await p.click('#aiToggle'); await sleep(600);
  await p.fill('textarea.ai-input', '三个样品的膜厚是怎么变的'); await p.press('textarea.ai-input', 'Enter');
  await sleep(2000);
  let sys = lastSystem(log);
  const nSamples = (sys.match(/"来自对比"/g) || []).length;
  const otLine = (sys.match(/"光学厚度 OT = n·d·cosθ \(nm\)": "([^"]+)"/) || [])[1] || '';
  console.log(`${sys.includes('"曲线摘要"') && nSamples === 3 ? 'PASS' : 'FAIL'}  抽屉提问带曲线摘要   样品 ${nSamples} 个；一条 OT 曲线 ${otLine.split(' ').length} 段：${otLine.slice(0, 60)}…`);
  console.log(`      模型收到的文字总长 ${sys.length} 字符；含「OT 可信比例」=${sys.includes('OT 可信比例')}；含读法说明=${sys.includes('对照「OT 可信比例」')}`);

  // 2. 样品页上的 ⓘ：范围是这一个样品
  await p.goto('http://127.0.0.1:8799/#sample/' + sid, { waitUntil: 'networkidle' });
  await sleep(2500);
  const dots = await p.$$('#page-sample .info-dot:visible');
  let asked = false;
  for (const d of dots) {
    await d.click(); await sleep(400);
    const box = await p.$('.info-pop textarea.info-input');
    if (box) {
      await box.fill('这一段为什么不可信'); await box.press('Enter');
      await sleep(300);
      asked = true; break;
    }
    await p.keyboard.press('Escape');
  }
  await sleep(2000);
  sys = lastSystem(log);
  const n2 = (sys.match(/"来自对比"/g) || []).length;
  console.log(`${asked && n2 === 1 ? 'PASS' : 'FAIL'}  样品页 ⓘ 提问带这一个样品的曲线   提问了=${asked}，曲线摘要里的样品数 ${n2}`);
  console.log(`errors: ${JSON.stringify(errs)}`);
  await b.close();
})();
