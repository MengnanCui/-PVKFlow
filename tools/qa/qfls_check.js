// QFLS 成像页（P-54 … P-58）的验收：从空工作区开始，点「生成演示数据并处理」，然后把五个界面走一遍。
//
// 准备：一个**新的**工作区（不需要假网关）。
//   HTE_WORKSPACE=/tmp/hte-qfls HTE_PORT=8799 HTE_NO_BROWSER=1 .venv/bin/python -m app.main
//   NODE_PATH=$(npm root -g) node tools/qa/qfls_check.js /tmp/hte-qfls
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8799/';
const [ws = '/tmp/hte-qfls'] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = [];
const rec = (name, ok, detail) => { out.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`); };

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('PAGEERR ' + e.message));
  await p.goto(BASE + '#qfls', { waitUntil: 'networkidle' });
  await sleep(800);

  // 1. 空工作区 → 生成演示数据 → 进度 → 出结果
  const firstUse = (await p.textContent('#page-qfls')).includes('还没有处理过 QFLS 数据');
  await p.click('#page-qfls button:has-text("生成演示数据并处理")');
  await p.click('.modal button:has-text("生成并处理")');
  let sawProgress = false;
  for (let i = 0; i < 180 && !(await p.locator('#page-qfls .qfls-tabs').count()); i++) {
    sawProgress ||= (await p.locator('#page-qfls .progress-card').count()) > 0;
    await sleep(1000);
  }
  await sleep(1200);
  const kpi = await p.$$eval('#page-qfls .metric-value', (xs) => xs.map((x) => x.textContent));
  rec('演示数据生成并处理', firstUse && sawProgress && kpi[0] === '60' && kpi[1] === '58',
      `空状态 ${firstUse}、看到进度 ${sawProgress}；样点 ${kpi[0]}、有效 ${kpi[1]}`);

  // 2. 总览：扫描报告（乱码目录修好）、按物镜分开的箱线图宽度跟着容器
  const scanTxt = await p.textContent('#page-qfls');
  const boxes = await p.$$eval('#page-qfls .qfls-grid .figure', (fs) => fs.map((f) => [f.querySelector('svg')?.viewBox.baseVal.width, f.clientWidth]));
  rec('总览：扫描报告 + 箱线图', scanTxt.includes('钝化剂筛选') && scanTxt.includes('显示时修成了 UTF-8') && scanTxt.includes('跳过 6 处')
      && boxes.length === 2 && boxes.every(([w, c]) => Math.abs(w - c) < 10) && scanTxt.includes('不能直接比较'),
      `箱线图 ${boxes.map(([w, c]) => `${w}/${c}`).join('、')}`);

  // 3. 筛选：物镜 100X + 只看异常；导出当前筛选
  await p.click('#page-qfls .qfls-filter button.chip-toggle:has-text("100X")'); await sleep(300);
  const n100 = await p.textContent('#page-qfls .qfls-filter');
  const groupRows = await p.$$eval('#page-qfls table.data tbody tr', (xs) => xs.filter((x) => x.closest('.section')?.textContent.includes('等级分布')).map((x) => x.children[1]?.textContent));
  await p.click('#page-qfls .qfls-filter button.chip-toggle:has-text("只看异常")'); await sleep(300);
  const nAnom = (await p.textContent('#page-qfls .qfls-filter')).match(/筛出 (\d+) \/ 60/)[1];
  // headless Chromium 对 blob 下载一律报 suggestedFilename = "download"（见 a2_download_name.js），文件名从 <a download> 上读
  await p.evaluate(() => { const o = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { window.__dlName = this.download; return o.call(this); }; });
  const [dl] = await Promise.all([p.waitForEvent('download'), p.click('#page-qfls button:has-text("导出当前筛选")')]);
  const dlName = await p.evaluate(() => window.__dlName);
  const csv = fs.readFileSync(await dl.path(), 'utf8');
  const lines = csv.trim().split('\n');
  rec('筛选 + 导出', n100.includes('筛出 28 / 60') && groupRows.every((o) => o === '100X') && lines.length === +nAnom + 1
      && csv.startsWith('\ufeff') && lines.slice(1).every((l) => l.includes('100X')) && dlName.endsWith(`筛选_${nAnom}点.csv`),
      `100X 28 个、只看异常 ${nAnom} 个；CSV ${lines.length - 1} 行（${dlName}）`);
  await p.click('#page-qfls button:has-text("清空筛选")'); await sleep(300);

  // 4. 排名：异常整行标色、无效的排最后；点表头换排序
  await p.click('#page-qfls .qfls-tab:has-text("排名与筛选")'); await sleep(800);
  const rk = await p.$$eval('#page-qfls table.qfls-rank tbody tr', (xs) => xs.map((x) => [x.className, x.children[1].textContent, x.children[6].textContent]));
  await p.click('#page-qfls table.qfls-rank th:has-text("Moran")'); await sleep(400);
  const mi = await p.$$eval('#page-qfls table.qfls-rank tbody tr', (xs) => xs.slice(0, 5).map((x) => +x.children[14].textContent));
  rec('排名表', rk.length === 60 && rk[0][1].startsWith('1') && rk.slice(-2).every((r) => r[2] === '无效' && r[0].includes('lv-error'))
      && rk.some((r) => r[0].includes('lv-warn')) && mi.every((v, i) => i === 0 || v >= mi[i - 1]),
      `第一名「${rk[0][1]}」；末两行无效；按 Moran's I 升序（越小越均匀）${mi.join(' ≤ ')}`);
  await p.click('#page-qfls table.qfls-rank th:has-text("分")'); await sleep(400);

  // 5. 单点详情：图都加载出来；上一个 / 下一个
  await p.locator('#page-qfls table.qfls-rank tbody tr').nth(2).click(); await sleep(2500);
  const imgs = await p.$$eval('#page-qfls-point img', (xs) => xs.map((x) => x.naturalWidth));
  const code1 = await p.textContent('#page-qfls-point .qfls-code');
  await p.click('#page-qfls-point button:has-text("下一个")'); await sleep(1500);
  const code2 = await p.textContent('#page-qfls-point .qfls-code');
  rec('单点详情', imgs.length === 5 && imgs.every((w) => w > 100) && code1 !== code2
      && (await p.textContent('#page-qfls-point')).includes('综合分怎么来的'),
      `${imgs.length} 张图都加载了；${code1} → 下一个 ${code2}`);

  // 6. 多点对比：一个 20X 一个 100X → 提示不可比；同物镜色标热图 + 三张叠加图
  await p.goto(BASE + '#qfls/rank', { waitUntil: 'networkidle' }); await sleep(1200);
  const pick = async (obj) => {
    const i = await p.$$eval('#page-qfls table.qfls-rank tbody tr', (xs, o) => xs.findIndex((x) => x.children[4].textContent === o && !x.querySelector('input').checked), obj);
    await p.locator('#page-qfls table.qfls-rank tbody tr').nth(i).locator('input').check(); await sleep(250);
  };
  await pick('20X'); await pick('100X');
  await p.click('#page-qfls button:has-text("对比所选")'); await sleep(3000);
  const cmp = await p.textContent('#page-qfls');
  const heat = await p.$$eval('#page-qfls .qfls-heat', (xs) => xs.map((x) => x.naturalWidth));
  const charts = await p.$$eval('#page-qfls .qfls-grid .figure', (fs) => fs.map((f) => f.querySelector('svg').viewBox.baseVal.width - f.clientWidth));
  rec('多点对比', cmp.includes('这几个样点物镜不同') && heat.length === 2 && heat.every((w) => w > 100) && charts.length === 3
      && charts.every((d) => Math.abs(d) < 10) && cmp.includes('指标对照'),
      `热图 ${heat.length}、叠加图 ${charts.length}（宽度贴合容器）`);

  // 7. 方法说明：阈值来自这次处理
  await p.click('#page-qfls .qfls-tab:has-text("方法说明")'); await sleep(600);
  const m = await p.textContent('#page-qfls');
  rec('方法说明', m.includes('> 0.15 提示结构不均') && m.includes('低于 1.1 eV') && m.includes('不做伪 JV') && m.includes('不代表任何真实材料'),
      '阈值、限制、演示数据声明都在');

  // 8. 处理新数据：粘贴路径 → 扫描预览
  await p.click('#page-qfls button:has-text("处理新数据")'); await sleep(500);
  await p.fill('.modal input.input.mono', path.join(ws, 'qfls_demo'));
  await p.click('.modal button:has-text("打开")'); await sleep(800);
  await p.click('.modal button:has-text("扫描这个目录")'); await sleep(1500);
  const pv = await p.textContent('.modal');
  rec('扫描预览', pv.includes('认出 3 个实验、60 帧') && pv.includes('修过的乱码名') && !(await p.locator('.modal button:has-text("开始处理")').isDisabled()),
      (pv.match(/认出[^。]*。/) || [''])[0]);
  await p.click('.modal button:has-text("取消")');

  // 原始数据一个字节没动：输出在 qfls/ 下，演示数据目录里没有多出文件
  const extra = fs.readdirSync(path.join(ws, 'qfls'));
  rec('输出在独立目录', extra.length === 1 && fs.existsSync(path.join(ws, 'qfls', extra[0], 'points.csv')), `qfls/${extra[0]}`);

  rec('零报错', errs.length === 0, errs.slice(0, 2).join(' | ') || '无');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
