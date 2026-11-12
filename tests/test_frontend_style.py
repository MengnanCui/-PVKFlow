"""前端代码的两条机械规矩（技术债 C076）。

- 一行不超过 140 个字符。原来新写的几页（QFLS、智能体）有一百多行超长，编辑器里要横着拖；
  `glossary.js` 例外 —— 它是术语表正文，一条定义一行，拆开反而难对照审稿清单
- 下拉、分段、指标卡只有一份（ui.js / components/figure.js）：页面里不再自己拼 `div.metric` 或 `.agent-seg`
"""
from __future__ import annotations

import re

from app import config

JS = config.ROOT / "web" / "js"
MAX = 140
LONG_OK = {"glossary.js"}


def test_no_js_line_is_longer_than_140_characters():
    long = [f"{p.relative_to(JS).as_posix()}:{i}（{len(line)}）"
            for p in sorted(JS.rglob("*.js")) if p.name not in LONG_OK
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if len(line) > MAX]
    assert not long, "超过 140 个字符的行：\n" + "\n".join(long)


def test_shared_widgets_are_not_copied_back_into_pages():
    hits = []
    for p in sorted((JS / "pages").glob("*.js")):
        src = p.read_text(encoding="utf-8")
        for pat in (r"h\(\s*'div\.metric'", r"agent-seg", r"^export function select\(", r"function seg\("):
            if re.search(pat, src, re.M):
                hits.append(f"{p.name}: {pat}")
    assert not hits, "页面里又抄了一份共用组件（用 ui.metric / ui.segmented / figure.selectControl）：\n" + "\n".join(hits)
