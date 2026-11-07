"""前端：能用鼠标点的东西，键盘也要够得着。

按钮、链接、表单控件天生能用键盘操作；别的元素（行、卡片……）挂了点击事件，
就得自己给 `tabindex` 和 `onkeydown`。原来样品行、对比历史的行都只能用鼠标点
（自查 §6 #30，P12 之后又补了对比历史）。这条测试把新写出来的「只能鼠标点」拦在提交之前。
"""
from __future__ import annotations

import re

from app import config

JS = config.ROOT / "web" / "js"
NATIVE = {"button", "a", "input", "select", "textarea", "option"}

# 挂了点击、但本来就不是给人点的 —— 每一条写清楚为什么
EXEMPT = {
    ("components/info.js", "div.info-pop"): "只是挡住冒泡：浮层里点一下不该被「点外面就关」逮到",
    ("ui.js", "div.modal-backdrop"): "点遮罩关弹窗；键盘用 Esc 关（ui.modal 里）",
}


def _clickables():
    for path in sorted(JS.rglob("*.js")):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(JS).as_posix()
        for m in re.finditer(r"h\(\s*[`'\"]([a-z0-9]+)([^`'\"]*)[`'\"]\s*,\s*\{", src):
            if m.group(1) in NATIVE:
                continue
            depth = 0
            for j in range(m.end() - 1, len(src)):
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                if depth == 0:
                    break
            props = src[m.end() - 1:j + 1]
            if re.search(r"\b(onclick|ondblclick|onpointerdown|onmousedown)\b", props):
                line = src[:m.start()].count("\n") + 1
                tag = m.group(1) + re.sub(r"#[\w-]+", "", m.group(2))
                yield rel, line, tag, props


def test_everything_clickable_can_be_reached_with_the_keyboard():
    found = list(_clickables())
    assert found, "一个都没找到 —— 扫描写坏了"
    missing = [f"{rel}:{line} <{tag}>" for rel, line, tag, props in found
               if (rel, tag) not in EXEMPT
               and not ("tabindex" in props and "onkeydown" in props)]
    assert missing == [], "这些只能用鼠标点：" + "；".join(missing)


def test_exemptions_still_exist():
    """豁免的元素改名或删了，豁免也要跟着删 —— 不然它会悄悄豁免掉别的东西。"""
    found = {(rel, tag) for rel, _line, tag, _props in _clickables()}
    assert set(EXEMPT) <= found, set(EXEMPT) - found
