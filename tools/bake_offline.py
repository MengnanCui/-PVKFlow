"""把整个界面烤成**一个** .html —— 双击就开，不用起服务、不用装 Python。

为什么要这东西：现在想看一眼界面只有一条路 —— 装 Python、起 FastAPI、开浏览器。
想在没环境的机器上看、想把界面发给别人指指点点，都做不到。

做法分两步：
  1. 录：起本地服务，用无头浏览器走一遍主要页面，把 `/api/**` 的**真实响应**
     全部存下来（JSON 存文本，热力图 PNG 和 frames.bin 存 base64）。
  2. 打：CSS / 字体 / JS 全部内联，再装一层「离线」拦截 —— fetch 从录好的那份取。

两条设计底线：

**不改 web/ 和 app/ 里任何一行。** 离线版是从现在跑着的代码烤出来的，
不是另一份实现。一旦要改源码才能烤，烤出来的就不是你正在跑的那个界面了。

**做不到的事要明说。** 重跑批处理、存模型配置、导入文件、AI 对话 —— 这些
离线版真的做不到。它们会返回一条「这是离线版」的错误，由界面照常显示出来。
静默失败是离线版最容易骗人的地方：让人以为功能坏了，跑去查一个不存在的 bug。

用法：
    .venv/bin/python tools/bake_offline.py                 # 烤到 dist/
    .venv/bin/python tools/bake_offline.py --out x.html
    .venv/bin/python tools/bake_offline.py --skip-record    # 复用上次录的
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
JS = WEB / "js"

# index.html 里 <link> 的顺序就是层叠顺序，不能乱
CSS_FILES = ["tokens.css", "base.css", "layout.css", "components.css"]

# ---------------------------------------------------------------- 打包 JS
#
# 为什么要自己写而不是上 esbuild/rollup：这份前端**没有 node_modules**，
# 为了烤一个离线版去引一整条 JS 工具链，是把「零依赖」这条最重要的性质丢掉。
#
# 而且这份代码的模块语法窄得刚好够手写：只有 `import { a, b } from '…'` 和
# `import * as ns from '…'` 两种进、`export function/const/class` 一种出，
# 没有动态 import()、没有 import.meta、没有 export default，**而且无环**。
# 无环意味着依赖总是先于使用者求值，所以不用处理活绑定和提升 ——
# 一个注册表加一层 IIFE 就够了。（这两条由 _check_assumptions 每次现场复核。）

IMPORT_RE = re.compile(
    r"^import\s+(?:(\*\s+as\s+(\w+))|(\{[\s\S]*?\}))\s+from\s+'([^']+)';?[ \t]*$",
    re.M)
EXPORT_DECL_RE = re.compile(
    r"^export\s+(?=(?:async\s+)?function|const|let|var|class)", re.M)
# 名字里要带上 `$` 和 `_` —— ui.js 导出的 `$` / `$$` 是真实存在的，
# 用 \w+ 会把它们漏掉，症状是打包后启动就抛「$ is not a function」。
EXPORT_NAME_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([\w$]+)", re.M)
EXPORT_LIST_RE = re.compile(r"^export\s+\{([^}]*)\};?[ \t]*$", re.M)


def _mod_key(path: pathlib.Path) -> str:
    return path.resolve().relative_to(JS.resolve()).as_posix()


def _deps(path: pathlib.Path, src: str) -> list[str]:
    return [_mod_key(path.parent / m.group(4)) for m in IMPORT_RE.finditer(src)]


def _order(entry: str, graph: dict[str, list[str]]) -> list[str]:
    """拓扑序，深度优先后序。无环是前提，_check_assumptions 保证。"""
    out: list[str] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        for d in graph[n]:
            visit(d)
        out.append(n)

    visit(entry)
    return out


def _check_assumptions(sources: dict[str, str], graph: dict[str, list[str]]) -> None:
    """把这个打包器赖以成立的三条前提**现场验一遍**。

    以后有人加一个 `export default` 或者写出一个环，这里当场报错，
    而不是烤出一个静默坏掉的 html —— 那种产物比没有更糟，
    你会对着它查一个源码里根本不存在的 bug。
    """
    bad = []
    for key, src in sources.items():
        if "export default" in src:
            bad.append(f"{key}：有 export default，这个打包器不认")
        if re.search(r"\bimport\s*\(", src):
            bad.append(f"{key}：有动态 import()，这个打包器不认")
        if "import.meta" in src:
            bad.append(f"{key}：用了 import.meta，内联之后没有意义")
        # import 语句必须全部被 IMPORT_RE 吃掉，漏一条就会留下裸 import 语法
        for line in re.findall(r"^import\b.*$", src, re.M):
            if not any(line in m.group(0) for m in IMPORT_RE.finditer(src)):
                bad.append(f"{key}：这条 import 解析不了 —— {line.strip()}")

    color: dict[str, int] = {}

    def dfs(n: str, stack: list[str]) -> None:
        color[n] = 1
        for d in graph[n]:
            if color.get(d) == 1:
                cyc = stack[stack.index(d):] + [d] if d in stack else [n, d]
                bad.append("循环依赖：" + " → ".join(cyc))
            elif color.get(d, 0) == 0:
                dfs(d, stack + [d])
        color[n] = 2

    for n in graph:
        if color.get(n, 0) == 0:
            dfs(n, [n])

    if bad:
        raise SystemExit("烤不了，前提不成立：\n  " + "\n  ".join(sorted(set(bad))))


def _transform(key: str, path: pathlib.Path, src: str) -> str:
    """一个模块 → 一个 IIFE。import 从注册表取，export 在末尾一次交出去。"""
    names: list[str] = []

    def sub_import(m: re.Match) -> str:
        dep = json.dumps(_mod_key(path.parent / m.group(4)))
        if m.group(1):                                   # import * as ns
            return f"const {m.group(2)} = __req({dep});"
        # import { a, b as c } —— 解构语法和对象字面量只差一个 `as`→`:`
        inner = m.group(3)[1:-1]
        parts = []
        for item in inner.split(","):
            item = item.strip()
            if not item:
                continue
            bits = item.split(" as ")
            parts.append(item if len(bits) == 1
                         else f"{bits[0].strip()}: {bits[1].strip()}")
        return f"const {{ {', '.join(parts)} }} = __req({dep});"

    body = IMPORT_RE.sub(sub_import, src)
    names += EXPORT_NAME_RE.findall(body)
    for m in EXPORT_LIST_RE.finditer(body):
        names += [x.strip() for x in m.group(1).split(",") if x.strip()]
    body = EXPORT_LIST_RE.sub("", body)
    body = EXPORT_DECL_RE.sub("", body)

    ret = ", ".join(dict.fromkeys(names))
    return (f"__def({json.dumps(key)}, function (__req) {{\n"
            f"{body}\nreturn {{ {ret} }};\n}});\n")


def bundle_js() -> str:
    sources = {_mod_key(p): p.read_text(encoding="utf-8")
               for p in sorted(JS.rglob("*.js"))}
    paths = {_mod_key(p): p for p in sorted(JS.rglob("*.js"))}
    graph = {k: _deps(paths[k], s) for k, s in sources.items()}
    _check_assumptions(sources, graph)

    order = _order("app.js", graph)
    parts = ["""// —— 极小模块注册表 ——
// 依赖无环，而且按拓扑序排过，所以 __req 拿到的一定是已经建好的那份。
var __mods = {}, __built = {};
function __def(id, fn) { __mods[id] = fn; }
function __req(id) {
  if (!(id in __built)) {
    if (!(id in __mods)) throw new Error('离线版里没有这个模块：' + id);
    __built[id] = __mods[id](__req);
  }
  return __built[id];
}
"""]
    parts += [_transform(k, paths[k], sources[k]) for k in order]
    parts.append("__req('app.js');\n")
    return "\n".join(parts), len(order)


# ---------------------------------------------------------------- 录 API
#
# 用无头浏览器走一遍真实页面，而不是照着 api.js 猜一份接口清单 ——
# 猜的那份迟早会和实际请求对不上（少一个参数、多一次预热），
# 而对不上的症状是离线版某一块空白，你会以为是界面坏了。

RECORD_JS = r"""
import { chromium } from 'playwright';
import fs from 'fs';
const PORT = process.argv[2], OUT = process.argv[3];
const ROUTES = JSON.parse(process.argv[4]);

const b = await chromium.launch(process.env.PW_CHROMIUM
  ? { executablePath: process.env.PW_CHROMIUM } : {});
const page = await b.newPage({ viewport: { width: 1600, height: 1000 } });

const snap = {};
page.on('response', async (res) => {
  const u = new URL(res.url());
  if (!u.pathname.startsWith('/api/')) return;
  const key = res.request().method() + ' ' + u.pathname + u.search;
  if (snap[key]) return;
  const ct = res.headers()['content-type'] || '';
  try {
    const buf = await res.body();
    const text = /json|text|javascript/.test(ct);
    snap[key] = {
      status: res.status(), ct,
      b64: text ? null : buf.toString('base64'),
      text: text ? buf.toString('utf8') : null,
    };
  } catch { /* 有的响应体拿不到（重定向、被取消），跳过就好 */ }
});

for (const r of ROUTES) {
  await page.goto(`http://127.0.0.1:${PORT}/${r.hash}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(r.wait || 4000);
  for (const step of (r.steps || [])) {
    try {
      if (step.click) await page.click(step.click, { timeout: 3000 });
      if (step.eval) await page.evaluate(step.eval);
      // 在**页面里**发请求，不在 node 里 —— URLSearchParams 的编码要和界面
      // 真正发出去的那一份逐字一致，否则快照的键对不上，
      // 离线版会说「没录进来」而其实录了。
      if (step.fetch) await page.evaluate(async (jobs) => {
        for (const [path, params] of jobs) {
          // **必须和 api.js 用同一套编码。** 那边是
          //   get(path, {windows: '0:1,3:4'})  →  URLSearchParams
          // 会把 `:` 和 `,` 转成 %3A / %2C。这边要是拼原始字符串，
          // 录下来的键和界面真正发出去的对不上 —— 症状是明明录了，
          // 离线版却说「没录进来」。
          try { await fetch(path + '?' + new URLSearchParams(params)); } catch (e) { /* 记下就行 */ }
        }
      }, step.fetch);
      await page.waitForTimeout(step.wait || 1500);
    } catch (e) { console.error('  跳过一步：', JSON.stringify(step), e.message.split('\n')[0]); }
  }
}
fs.writeFileSync(OUT, JSON.stringify(snap));
console.error(`  录到 ${Object.keys(snap).length} 条响应`);
await b.close();
"""

# 走哪几条路。第一条要把侧栏四个页面都点一遍 —— 它们是 SPA 内部切换，
# 不重新加载页面，直接 goto hash 反而可能因为「已经在这一页」而不重画。
ROUTES = [
    {"hash": "", "wait": 4000, "steps": [
        {"click": '.nav-item[data-page="process"]', "wait": 3000},
        {"click": '.nav-item[data-page="storage"]', "wait": 2500},
        {"click": '.nav-item[data-page="relation"]', "wait": 2500},
        {"click": '.nav-item[data-page="history"]', "wait": 2500},
        {"click": '.nav-item[data-page="settings"]', "wait": 3000},
        {"click": '.nav-item[data-page="overview"]', "wait": 2000},
    ]},
]


def _routes(run_id: str, artifact_id: str, t_max: float) -> list[dict]:
    out = list(ROUTES)
    # 对比页那根时刻滑块是这一轮刚做的东西，离线版里得**真能拖** ——
    # 拖不动就没法查它的问题。整条时间轴每一秒各录一个窗口：
    # 一条响应才一两 KB，二十来秒也就三十几 KB，比一张热力图还小。
    out.append({
        "hash": f"#batch/{run_id}", "wait": 6000,
        "steps": [{"fetch": [[f"/api/batch/runs/{run_id}/slices",
                              {"windows": f"0:1,{n}:{n + 1}"}]
                             for n in range(0, max(1, int(t_max)))], "wait": 2500}],
    })
    out.append({"hash": f"#sample/{artifact_id}", "wait": 9000})
    return out


def _pick_targets(port: int) -> tuple[str, str, float]:
    """录哪一次对比、哪一个样品 —— 问服务，不写死 id。

    写死 id 的话，换一台机器、换一份工作区就烤不出来了。
    """
    import urllib.request

    def get(path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
            return json.load(r)

    runs = get("/api/batch/runs").get("runs") or []
    # 挑样品最多的那次：空跑的那几次没有图可看，烤出来是一页空白
    runs = sorted(runs, key=lambda r: r.get("n_children") or 0, reverse=True)
    if not runs:
        raise SystemExit("工作区里没有跑过对比，没什么可烤的。先跑一次批处理。")

    samples = get("/api/spectra/samples").get("samples") or []
    matrices = [s["matrices"][0]["artifact_id"] for s in samples if s.get("matrices")]
    if not matrices:
        raise SystemExit("工作区里没有光谱矩阵，样品页烤不出来。")
    run_id = runs[0]["analysis_run_id"]
    # 这批数据测到第几秒 —— 决定时刻滑块要录到哪儿。问服务，不猜。
    rows = get(f"/api/batch/runs/{run_id}/slices?windows=0:1").get("rows") or []
    t_max = max((r.get("t_max") or 0) for r in rows) if rows else 0.0
    return run_id, matrices[0], float(t_max)


# ---------------------------------------------------------------- 离线层
#
# 装在 bundle **之前**：app.js 一跑起来第一件事就是打接口，晚一步都来不及。

SHIM_JS = r"""
(function () {
  var SNAP = window.__SNAPSHOT__ || {};
  var BAKED_AT = window.__BAKED__ || {};

  function keyOf(method, url) {
    var u; try { u = new URL(url, location.href); } catch (e) { return method + ' ' + url; }
    return method + ' ' + u.pathname + u.search;
  }
  /** **只认完全相同的地址。**
   *
   * 「找不到就退回按路径匹配」听起来友好，实际上是这个离线版能犯的最严重的错：
   * 你把时刻滑块拖到 5 s，快照里只有 18–19 s，按路径一匹配就把 18 秒的数字
   * 顶着「5–6 s」的标题画出来 —— 你会对着一组错的数下结论。
   * 这份前端也没有缓存击穿参数，本来就不需要模糊匹配。没录到就明说没录到。 */
  function look(method, url) {
    return SNAP[keyOf(method, url)] || null;
  }

  function bytes(b64) {
    var s = atob(b64), a = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) a[i] = s.charCodeAt(i);
    return a;
  }

  // ---- 一、fetch
  //
  // 没录到的接口返回 **这个平台自己那套错误结构** —— errorBox 会把这句话
  // 画在该出现的位置上。返回一个空对象或者静默失败的话，你会看到一块空白，
  // 然后跑去查一个源码里根本不存在的 bug。
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (url.indexOf('/api/') === -1) return realFetch(input, init);

    var hit = look(method, url);
    if (hit) {
      var body = hit.text !== null && hit.text !== undefined ? hit.text : bytes(hit.b64);
      return Promise.resolve(new Response(body, {
        status: hit.status, headers: { 'Content-Type': hit.ct || 'application/json' },
      }));
    }
    var why = method === 'GET'
      ? '这是离线版，' + url.split('?')[0] + ' 没有录进快照。'
      : '这是离线版，做不了「' + method + ' ' + url.split('?')[0] + '」这种会改数据的操作。';
    return Promise.resolve(new Response(JSON.stringify({
      error: { message: why + '要真跑这一步，得起本地服务（run.bat / run.sh）。',
               kind: 'offline', detail: '' },
    }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
  };

  // ---- 二、图片地址
  //
  // 热力图走 SVG <image href> 和 new Image().src，**都不经过 fetch**。
  // 只拦 fetch 的话，离线版里三张热力图全是空的 —— 而那恰好是这个平台
  // 最显眼的一块。所以在设属性这一层再拦一道，img/svg/CSS 全都管住。
  function dataURI(url) {
    if (!url || String(url).indexOf('/api/') === -1) return null;
    var hit = look('GET', String(url));
    if (!hit || !hit.b64) return null;
    return 'data:' + (hit.ct || 'image/png') + ';base64,' + hit.b64;
  }
  var setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (name === 'src' || name === 'href' || name === 'xlink:href') {
      var d = dataURI(value);
      if (d) return setAttr.call(this, name, d);
    }
    return setAttr.call(this, name, value);
  };
  ['HTMLImageElement', 'SVGImageElement'].forEach(function (n) {
    var C = window[n]; if (!C) return;
    var prop = n === 'HTMLImageElement' ? 'src' : 'href';
    var desc = Object.getOwnPropertyDescriptor(C.prototype, prop);
    if (!desc || !desc.set) return;
    Object.defineProperty(C.prototype, prop, {
      configurable: true, enumerable: desc.enumerable, get: desc.get,
      set: function (v) { desc.set.call(this, dataURI(v) || v); },
    });
  });

  // ---- 三、横幅
  //
  // 找问题的人必须知道自己在看什么版本、什么时候烤的、哪些动作不作数。
  window.addEventListener('DOMContentLoaded', function () {
    var bar = document.createElement('div');
    bar.className = 'offline-bar';
    bar.innerHTML = '<b>离线版</b>　' + (BAKED_AT.commit || '') + '　' + (BAKED_AT.at || '')
      + '　<span>接口响应是烤制时录下来的真实数据。重跑批处理 / 存模型配置 / '
      + '导入文件 / AI 对话这几件事需要后端，点了会明说做不到。</span>'
      + '<button type="button" title="收起">×</button>';
    bar.querySelector('button').onclick = function () { bar.remove(); };
    document.body.insertBefore(bar, document.body.firstChild);
  });
})();
"""

# 装在 bundle **之后**：这几个接口离线下必然做不到，与其让它返回一个
# 点了没反应的地址，不如就地关掉 —— batch.js 早就认「地址为空」这个信号，
# 会显示「这个版本没有后端，打不出 zip」。
PATCH_JS = r"""
(function () {
  try {
    var api = __req('api.js').api;
    api.batchExportUrl = function () { return ''; };     // 打不出 zip，界面已认这个信号
    // 侧栏那行状态。录到的 /api/health 是真的（3 个 Skill、版本号都对），
    // 但顶着一个绿色的「本地服务正常」和横幅上的「离线版」自相矛盾 ——
    // 而红色的「连不上本地服务」又像是坏了。两个都不对，第三句才对。
    //
    // setTimeout(0) 排在 checkHealth 那个 await 之后，所以每次轮询完
    // 都会被我们改回来，包括 30 秒一次的那些。
    api.health = (function (orig) {
      return function () {
        setTimeout(function () {
          var el = document.querySelector('#healthStatus');
          if (el) { el.className = 'status status-idle small';
                    el.textContent = '离线版 · 没有本地服务'; }
        }, 0);
        return orig.apply(null, arguments);
      };
    })(api.health);
    // 头一次 checkHealth() 在 app.js 求值时就跑完了 —— 那时这个补丁还没打上，
    // 所以这里补一次，别等 30 秒后的下一轮。
    setTimeout(function () {
      var el = document.querySelector('#healthStatus');
      if (el) { el.className = 'status status-idle small';
                el.textContent = '离线版 · 没有本地服务'; }
    }, 300);
  } catch (e) { console.warn('离线补丁没打上：', e); }
})();
"""

OFFLINE_CSS = """
/* 离线版横幅。占一整行、不遮内容，配色跟着主题走。 */
.offline-bar { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
  padding: 7px 16px; font-size: 12px; line-height: 1.5;
  background: var(--accent-wash); color: var(--ink-2);
  border-bottom: 1px solid var(--line-2); }
.offline-bar b { color: var(--accent); }
.offline-bar span { color: var(--ink-3); }
.offline-bar button { margin-left: auto; border: 0; background: none; cursor: pointer;
  color: var(--ink-3); font-size: 15px; line-height: 1; padding: 0 4px; }
.offline-bar button:hover { color: var(--ink); }
.shell { min-height: 100vh; }
"""


# ---------------------------------------------------------------- 组装
def inline_css() -> str:
    """四个样式表按 index.html 里的顺序拼起来，字体换成 base64 内嵌。"""
    css = "\n".join((WEB / "css" / f).read_text(encoding="utf-8") for f in CSS_FILES)

    def sub_font(m: re.Match) -> str:
        p = WEB / m.group(1).lstrip("/").replace("assets/", "", 1)
        if not p.is_file():
            return m.group(0)
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"url('data:font/woff2;base64,{b64}')"

    css = re.sub(r"url\('([^']+\.woff2)'\)", sub_font, css)
    return css + OFFLINE_CSS


def guard_secrets(snapshot: dict) -> None:
    """产物里绝不能出现密钥或内网主机名。

    `/api/settings/models` 的密钥本来就是打码后才出后端的（`_redact()`，
    由 test_api_key_is_never_returned_in_full 那族测试盯着）。这里再断言一次 ——
    这个文件是要发出去的，多一道闸不亏。命中就中止，不出文件。
    """
    blob = json.dumps(snapshot, ensure_ascii=False)
    hits = []
    if re.search(r'"sk-[A-Za-z0-9_-]{6,}', blob):
        hits.append("快照里有像密钥的串（sk-…）")
    for host in re.findall(r'https?://([A-Za-z0-9.-]+)', blob):
        if host in ("127.0.0.1", "localhost") or host.endswith((
                ".openai.com", ".deepseek.com", ".bigmodel.cn", ".moonshot.cn",
                "openai.com", "www.w3.org", "creativecommons.org")):
            continue
        hits.append(f"快照里有一个不认识的主机名：{host}")
    if hits:
        raise SystemExit("不出文件 —— " + "；".join(sorted(set(hits))))


def build(snapshot: dict, out: pathlib.Path) -> None:
    js, n_mods = bundle_js()
    guard_secrets(snapshot)

    commit = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h %s"],
                            capture_output=True, text=True).stdout.strip()
    stamp = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cd",
                            "--date=format:%Y-%m-%d %H:%M"],
                           capture_output=True, text=True).stdout.strip()

    src = (WEB / "index.html").read_text(encoding="utf-8")
    body = src[src.index("<body>") + len("<body>"):src.index("<noscript>")]

    # head 原样搬过来，只把四个 <link rel=stylesheet> 换成一个内联 <style>。
    #
    # 别去正则单独抠 favicon —— 那个 <link> 的 href 是一段内联 SVG，里面**有 `>`**，
    # `[^>]*` 会在 SVG 中间就截断，剩下半截未闭合的属性把后面整个 <style> 都吞进去，
    # 症状是产物一条样式规则都没有（我第一版就是这么坏的）。
    head = src[src.index("<head>") + len("<head>"):src.index("</head>")]
    head = re.sub(r'^[ \t]*<link rel="stylesheet"[^\n]*\n', "", head, flags=re.M)
    head = head.replace("<title>HTE Studio</title>", "<title>HTE Studio · 离线版</title>")

    def blk(tag: str, text: str) -> str:
        # 字符串里出现 </script> 会把标签提前关掉 —— 转义那个斜杠，
        # JS 里 "<\\/script>" 和 "</script>" 是同一个串
        safe = text.replace("</script>", "<\\/script>")
        return "<" + tag + ">\n" + safe + "\n</" + tag + ">"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
{head}
<style>
{inline_css()}
</style>
</head>
<body>
{blk("script", "window.__SNAPSHOT__ = " + json.dumps(snapshot, ensure_ascii=False) + ";")}
{blk("script", "window.__BAKED__ = " + json.dumps({"commit": commit, "at": stamp}, ensure_ascii=False) + ";")}
{blk("script", SHIM_JS)}
{body}
<noscript><p style="padding:24px">这个界面需要 JavaScript。请用 Chrome / Edge / Firefox 打开。</p></noscript>
{blk("script", js)}
{blk("script", PATCH_JS)}
</body>
</html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    mb = out.stat().st_size / 1024 / 1024
    print(f"  {out}　{mb:.1f} MB　（{n_mods} 个模块 · {len(snapshot)} 条接口响应）")


def _playwright_dir() -> pathlib.Path:
    """找到装着 playwright 的那个目录。

    ESM 的裸模块解析只看脚本所在目录往上的 node_modules —— NODE_PATH 对它无效，
    所以录制脚本必须写进这个目录里跑。
    """
    cands = [pathlib.Path(os.environ["PLAYWRIGHT_DIR"])] if os.environ.get("PLAYWRIGHT_DIR") else []
    cands += [ROOT, pathlib.Path.cwd(), pathlib.Path("/tmp"), pathlib.Path.home()]
    for d in cands:
        if (d / "node_modules" / "playwright").is_dir():
            return d
    raise SystemExit(
        "找不到 playwright（录接口要用它开无头浏览器）。\n"
        "  装一下：npm i playwright\n"
        "  或者用 PLAYWRIGHT_DIR=<装了 playwright 的目录> 指过去\n"
        "  已经录过的话，--skip-record 可以跳过这一步")


def _chromium() -> str:
    """这台机器上的 Chromium。找不到就交给 Playwright 自己挑默认的。"""
    if os.environ.get("PW_CHROMIUM"):
        return os.environ["PW_CHROMIUM"]
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                "/opt/pw-browsers/chromium/chrome-linux/chrome"):
        hit = sorted(pathlib.Path("/").glob(pat.lstrip("/")))
        if hit:
            return str(hit[-1])
    return ""


def record(port: int, cache: pathlib.Path) -> dict:
    import shutil
    import time
    import urllib.error
    import urllib.request

    node = shutil.which("node")
    if not node:
        raise SystemExit("要录接口得有 node（Playwright 在上面跑）。"
                         "已经录过的话可以 --skip-record 复用。")
    pw_dir = _playwright_dir()

    srv = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:app",
         "--port", str(port), "--host", "127.0.0.1"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        else:
            raise SystemExit("本地服务起不来，录不了。")

        run_id, artifact_id, t_max = _pick_targets(port)
        print(f"  录：对比 {run_id} · 样品矩阵 {artifact_id} · 时间轴 0–{t_max:g} s")

        # 脚本要放在**装着 playwright 的那个目录**下 —— ESM 的裸模块解析
        # 只认自己所在目录往上的 node_modules，NODE_PATH 对它无效。
        script = pw_dir / "_hte_record.mjs"
        script.write_text(RECORD_JS, encoding="utf-8")
        env = dict(os.environ)
        chrome = _chromium()
        if chrome:
            env["PW_CHROMIUM"] = chrome
        try:
            subprocess.run([node, str(script), str(port), str(cache),
                            json.dumps(_routes(run_id, artifact_id, t_max))],
                           cwd=pw_dir, check=True, env=env)
        finally:
            script.unlink(missing_ok=True)
    finally:
        srv.terminate()
        srv.wait(timeout=10)
    return json.loads(cache.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description="把界面烤成一个自包含的 .html")
    ap.add_argument("--out", default=str(ROOT / "dist" / "hte-studio.offline.html"))
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--cache", default=str(ROOT / "dist" / "_snapshot.json"))
    ap.add_argument("--skip-record", action="store_true",
                    help="复用上次录的快照，只重新打包（改了前端代码时用这个，很快）")
    a = ap.parse_args()

    cache = pathlib.Path(a.cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    if a.skip_record:
        if not cache.is_file():
            raise SystemExit(f"没有 {cache}，第一次得先录一遍（去掉 --skip-record）")
        snapshot = json.loads(cache.read_text(encoding="utf-8"))
        print(f"  复用上次录的 {len(snapshot)} 条响应")
    else:
        snapshot = record(a.port, cache)

    build(snapshot, pathlib.Path(a.out))


if __name__ == "__main__":
    main()
