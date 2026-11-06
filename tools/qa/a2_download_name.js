// #20 下载文件名：记下点下去那一刻 <a download> 上写的文件名。
// （headless Chromium 对 blob 下载一律报 suggestedFilename = "download"，
//   连手写的 <a download="x.csv"> 也一样，所以不能拿那个当判据。）
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await (await b.newContext()).newPage();
  await p.goto('http://127.0.0.1:8799/#sample/' + process.argv[2], { waitUntil: 'networkidle' });
  await new Promise((r) => setTimeout(r, 2500));
  await p.evaluate(() => {
    const orig = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { window.__dlName = this.download; return orig.call(this); };
  });
  const btn = (await p.$$('button:has-text("下载 ▾")'))[1];
  await btn.click();
  await new Promise((r) => setTimeout(r, 200));
  await p.click('text=下载数据（CSV）');
  await new Promise((r) => setTimeout(r, 500));
  const name = await p.evaluate(() => window.__dlName);
  console.log(`${name && name.startsWith('ZG00') ? 'PASS' : 'FAIL'}  #20 下载文件名带样品名     <a download> = ${name}`);
  await b.close();
})();
