"""从文件名 / 路径解析样品身份。

高通量实验里，样品身份基本都编码在文件名或目录名里。与其让人一条条手点，
不如把命名习惯写成规则，导入时先给一份匹配预览，人只需要处理没匹配上的少数。

规则语法（在设置页可改，按顺序匹配，第一条命中为准）：

    {batch}_{sample}_{method}   占位符按字面分隔符切分文件名主干
    {sample}-{method}           分隔符可以是任意字面字符
    {*}_{sample}                {*} 表示这一段存在但丢弃
    @parent                     内置规则：父文件夹名即 sample
    @parent2                    内置规则：祖父文件夹名即 sample（父文件夹当 method）

只有 {sample} 是必需的；{batch} / {method} 命中就用，没有就留空。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

FIELDS = ("batch", "sample", "method")
_PLACEHOLDER = re.compile(r"\{(\*|[a-zA-Z_][a-zA-Z0-9_]*)\}")

BUILTIN_RULES = ("@parent", "@parent2")


@dataclass(frozen=True)
class NameMatch:
    sample: str
    batch: str = ""
    method: str = ""
    rule: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.sample)


def compile_rule(template: str) -> re.Pattern[str] | None:
    """把模板编译成带命名组的正则。模板非法时返回 None，不抛异常拖垮导入。"""
    if not template or template.startswith("@"):
        return None

    parts: list[str] = []
    pos = 0
    names: list[str] = []
    for m in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        names.append(m.group(1))
        parts.append(None)  # type: ignore[arg-type]  # 占位，稍后填
        pos = m.end()
    tail = re.escape(template[pos:])

    if not names:
        return None

    # 除最后一个占位符外都用非贪婪，保证分隔符优先切分
    out: list[str] = []
    idx = 0
    anon = 0
    for chunk in parts:
        if chunk is not None:
            out.append(chunk)
            continue
        name = names[idx]
        last = idx == len(names) - 1
        quant = ".+" if last else ".+?"
        if name == "*":
            out.append(f"(?:{quant})")
        else:
            # 同名占位符出现多次时，后面的退化成反向引用避免正则报错
            if name in {n for n in names[:idx] if n != "*"}:
                anon += 1
                out.append(f"(?P<dup{anon}>{quant})")
            else:
                out.append(f"(?P<{name}>{quant})")
        idx += 1
    out.append(tail)

    try:
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def _apply_builtin(rule: str, rel_parts: list[str]) -> NameMatch | None:
    """@parent / @parent2：拿目录层级当身份。"""
    dirs = rel_parts[:-1]
    if rule == "@parent" and dirs:
        return NameMatch(sample=dirs[-1], rule=rule)
    if rule == "@parent2" and len(dirs) >= 2:
        return NameMatch(sample=dirs[-2], method=dirs[-1], rule=rule)
    return None


def parse(relative_path: str, rules: Iterable[str]) -> NameMatch:
    """按顺序试每条规则。全都没命中时返回 sample 为空的 NameMatch。"""
    rel = PurePosixPath(str(relative_path).replace("\\", "/"))
    parts = [p for p in rel.parts if p not in ("", ".", "/")]
    if not parts:
        return NameMatch(sample="")
    stem = PurePosixPath(parts[-1]).stem

    for rule in rules:
        if rule in BUILTIN_RULES:
            hit = _apply_builtin(rule, parts)
            if hit:
                return hit
            continue

        pattern = compile_rule(rule)
        if pattern is None:
            continue
        m = pattern.match(stem)
        if not m:
            continue
        groups = {k: (v or "").strip() for k, v in m.groupdict().items() if k in FIELDS}
        sample = groups.get("sample", "").strip()
        if not sample:
            continue
        return NameMatch(
            sample=sample,
            batch=groups.get("batch", ""),
            method=groups.get("method", ""),
            rule=rule,
        )

    return NameMatch(sample="")


def preview(paths: Iterable[str], rules: Iterable[str]) -> list[dict]:
    """导入前的匹配预览：每个文件解析成什么，让人一眼看出规则对不对。"""
    rules = list(rules)
    out = []
    for p in paths:
        m = parse(p, rules)
        out.append({
            "path": p,
            "sample": m.sample,
            "batch": m.batch,
            "method": m.method,
            "rule": m.rule,
            "matched": m.matched,
        })
    return out
