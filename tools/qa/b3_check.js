// B（三）的验收：自查 §6 剩下那几条里能在浏览器里量的。
//
// 用法（准备工作见 README.md；样品要多于一屏 —— 键盘那一条要看虚拟列表重画时焦点丢不丢）：
//   NODE_PATH=$(npm root -g) node tools/qa/b3_check.js smp_xxx run_xxx
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799/';
const [sid, rid] = process.argv.slice(2);
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
const active = (p) => p.evaluate(() => {
  const a = document.activeElement;
  return { index: a?.dataset?.index ?? null, inModal: !!a?.closest('.modal'), text: a?.textContent?.trim().slice(0, 20) };
});

(async () => {
  const b = await chromium.launch();

  // #30 键盘选样品
  { const { ctx, p } = await fresh(b, 'process');
    await sleep(1200);
    const roles = await p.evaluate(() => ({
      list: document.querySelector('.vlist')?.getAttribute('role'),
      row: document.querySelector('.vrow[data-index]')?.getAttribute('role'),
      tab: document.querySelector('.vrow[data-index]')?.tabIndex,
    }));
    rec('列表 / 行的角色', roles.list === 'listbox' && roles.row === 'option' && roles.tab === 0,
        `listbox=${roles.list}，option=${roles.row}，tabIndex=${roles.tab}`);
    await p.focus('.vrow[data-index="0"]');
    await p.keyboard.press('Space'); await sleep(150);
    const one = await p.textContent('#selectCount');
    for (let i = 0; i < 25; i++) { await p.keyboard.press('ArrowDown'); await sleep(40); }
    await sleep(300);
    const at = await active(p);
    rec('上下键走 25 行焦点不丢', at.index === '25', `焦点在第 ${at.index} 行（虚拟列表中途重画过）`);
    await p.keyboard.press('Space'); await sleep(100);
    await p.keyboard.press('Shift+ArrowDown'); await p.keyboard.press('Shift+ArrowDown'); await sleep(200);
    const four = await p.textContent('#selectCount');
    rec('空格 / Shift+下 选', one === '已选 1' && four === '已选 4', `空格后「${one}」，再选一行 + Shift 下两行后「${four}」`);
    await p.keyboard.press('Enter'); await sleep(1500);
    const h = await p.evaluate(() => location.hash);
    rec('回车打开样品', h.startsWith('#sample/'), `地址 ${h.slice(0, 24)}`);
    rec('处理页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  // #30 弹窗：dialog 角色、Tab 困在里面、关了焦点回到按钮
  { const { ctx, p } = await fresh(b, 'batch/' + rid);
    await sleep(2500);
    const opener = p.locator('#chartHost button:has-text("导出脚本")').first();
    await opener.focus(); await opener.press('Enter'); await sleep(500);
    const dlg = await p.evaluate(() => {
      const m = document.querySelector('.modal');
      const t = m && document.getElementById(m.getAttribute('aria-labelledby') || '');
      return { role: m?.getAttribute('role'), modal: m?.getAttribute('aria-modal'), name: t?.textContent };
    });
    rec('弹窗是 dialog', dlg.role === 'dialog' && dlg.modal === 'true' && dlg.name === '导出绘图脚本',
        `role=${dlg.role} aria-modal=${dlg.modal} 名字「${dlg.name}」`);
    let escaped = 0;
    for (let i = 0; i < 15; i++) { await p.keyboard.press(i % 4 === 3 ? 'Shift+Tab' : 'Tab'); if (!(await active(p)).inModal) escaped += 1; }
    rec('Tab 困在弹窗里', escaped === 0, `按 15 次 Tab / Shift+Tab，跑出弹窗 ${escaped} 次`);
    await p.keyboard.press('Escape'); await sleep(300);
    const back = await active(p);
    rec('关了焦点回到按钮', back.text === '导出脚本', `焦点在「${back.text}」`);
    await ctx.close(); }

  // #30 提示有 aria-live，出错的是 alert
  { const { ctx, p } = await fresh(b, 'overview');
    await sleep(800);
    const t = await p.evaluate(async () => {
      const ui = await import('/assets/js/ui.js');
      ui.toast('普通提示'); ui.toast('出错了', 'err');
      const st = document.querySelector('.toast-stack');
      const [a, c] = st.querySelectorAll('.toast');
      return { live: st.getAttribute('aria-live'), role: st.getAttribute('role'), a: a?.getAttribute('role'), c: c?.getAttribute('role') };
    });
    rec('提示读屏念得到', t.live === 'polite' && t.role === 'status' && t.a === null && t.c === 'alert',
        `容器 role=${t.role} aria-live=${t.live}；普通=${t.a}，出错=${t.c}`);
    await ctx.close(); }

  // #29 下载热力图：服务端出错时说出来，不存一个打不开的 png
  { const { ctx, p } = await fresh(b, 'sample/' + sid);
    await sleep(2500);
    await p.route('**/heatmap.png*', (route) => route.fulfill({ status: 500, contentType: 'application/json',
      body: JSON.stringify({ error: { message: '故意失败', kind: 'internal', detail: '' } }) }));
    let downloaded = 0;
    p.on('download', () => { downloaded += 1; });
    // 热力图那张的「下载 ▾」：它所在那一块的标题里有「热力图」
    const btn = (await p.evaluateHandle(() => [...document.querySelectorAll('button')]
      .filter((x) => x.textContent === '下载 ▾')
      .find((x) => (x.closest('.section, .panel, .figure')?.querySelector('.section-title, .figure-title, h3, h4')
        ?.textContent || '').includes('热力图')))).asElement();
    if (!btn) throw new Error('没找到热力图的下载按钮');
    await btn.click(); await sleep(200);
    await p.click('button.ai-menu-item:has-text("下载图片")'); await sleep(1200);
    const toasts = await p.$$eval('.toast', (xs) => xs.map((x) => x.textContent));
    rec('热力图下载失败要说', downloaded === 0 && toasts.some((x) => x.includes('下载失败') && x.includes('故意失败')),
        `下载了 ${downloaded} 个文件；提示「${toasts.join(' / ')}」`);
    await ctx.close(); }

  // #16 点「停止」之后：已经说出来的字落库，重新打开这个对话还在，标着「已停止」
  { const { ctx, p } = await fresh(b, 'process');
    await sleep(800);
    await p.click('#aiToggle'); await sleep(600);
    await p.fill('textarea.ai-input', '慢慢说'); await p.press('textarea.ai-input', 'Enter');
    await sleep(1200);
    await p.click('#aiDrawer button:has-text("停止")'); await sleep(2000);
    const saved = await p.evaluate(async () => {
      const list = await (await fetch('/api/chat/conversations')).json();
      const c = (list.conversations || list.rows || [])[0];
      const d = await (await fetch('/api/chat/conversations/' + c.conversation_id)).json();
      const m = (d.messages || []).filter((x) => x.role === 'assistant').pop();
      return { len: (m?.content || '').length, aborted: !!m?.meta?.aborted, pending: !!m?.meta?.pending };
    });
    await p.reload({ waitUntil: 'networkidle' }); await sleep(800);
    await p.click('#aiToggle'); await sleep(600);
    await p.click('.ai-head button[title="菜单"]'); await sleep(200);
    await p.click('button.ai-menu-item:has-text("历史对话")'); await sleep(600);
    // 每打开一次抽屉就有一个空的「新对话」排在最前面，点名字是「慢慢说」的那个
    await p.click('button.ai-histrow:has-text("慢慢说")'); await sleep(800);
    const body = await p.textContent('.ai-body');
    rec('停止后已说的字还在', saved.len > 0 && saved.aborted && !saved.pending && body.includes('（已停止）'),
        `库里 ${saved.len} 字、aborted=${saved.aborted}、pending=${saved.pending}；重开对话看得到「已停止」=${body.includes('（已停止）')}`);
    await ctx.close(); }

  // #28 构效关系页：字段全列出来，和标题的数一致
  { const { ctx, p } = await fresh(b, 'relation');
    await sleep(1200);
    // E 组之后这一页还有「逐个样品」的表 —— 只数「全部可分析的字段」那一节自己的表
    const r = await p.evaluate(() => {
      const sec = [...document.querySelectorAll('#page-relation .section')]
        .find((x) => x.querySelector('.section-title')?.textContent === '全部可分析的字段');
      return { note: sec?.querySelector('.section-note')?.textContent || '',
               rows: sec?.querySelectorAll('table.data tbody tr').length ?? -1 };
    });
    const n = Number((r.note.match(/(\d+) 个数值字段/) || [])[1]);
    rec('构效关系字段不截断', n > 0 && r.rows === n, `标题说 ${n} 个，表里 ${r.rows} 行`);
    rec('构效关系页零报错', p.errors.length === 0, p.errors.slice(0, 2).join(' | ') || '无');
    await ctx.close(); }

  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed} / ${out.length} 通过`);
  await b.close();
  process.exit(failed ? 1 : 0);
})();
