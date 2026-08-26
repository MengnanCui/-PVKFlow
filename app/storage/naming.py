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


# ------------------------------------------------------------------ 可枚举段识别
_NUM_SEG = re.compile(r"\d+")


@dataclass(frozen=True)
class Enumeration:
    """样品名里一段可枚举的数字。

    B20_S1 … B20_S48 → prefix="B20_S", suffix="", 1..48。

    这东西的用处：**把范围画成控件，而不是让人自己去查范围再回来输**。
    滑块的两个端点就是数据里的真实 min/max。
    """
    prefix: str
    suffix: str
    min: int
    max: int
    count: int          # 实际出现的不同数字个数
    width: int          # 零填充宽度，S001 是 3
    complete: bool      # min..max 中间有没有缺口

    def as_dict(self) -> dict:
        return {
            "prefix": self.prefix, "suffix": self.suffix,
            "min": self.min, "max": self.max, "count": self.count,
            "width": self.width, "complete": self.complete,
            "span": self.max - self.min + 1,
            "label": f"{self.prefix}[{self.min}–{self.max}]{self.suffix}",
        }


def detect_enumerations(names: Iterable[str], min_members: int = 3,
                        limit: int = 12) -> list[Enumeration]:
    """从一堆样品名里找出所有「前缀 + 数字 + 后缀」的模式。

    一个名字可能有多段数字（B20_S1 里的 20 和 1），每一段都单独成组。
    按覆盖的样品数排序 —— 覆盖得多的那个通常就是用户想要的那个。
    """
    groups: dict[tuple[str, str], dict] = {}

    for name in names:
        if not name:
            continue
        for m in _NUM_SEG.finditer(name):
            key = (name[:m.start()], name[m.end():])
            g = groups.setdefault(key, {"values": set(), "widths": set()})
            g["values"].add(int(m.group()))
            g["widths"].add(len(m.group()))

    out: list[Enumeration] = []
    for (prefix, suffix), g in groups.items():
        values = g["values"]
        if len(values) < min_members:
            continue
        lo, hi = min(values), max(values)
        out.append(Enumeration(
            prefix=prefix, suffix=suffix, min=lo, max=hi,
            count=len(values),
            width=max(g["widths"]) if len(g["widths"]) == 1 else 0,
            complete=len(values) == hi - lo + 1,
        ))

    # 覆盖的样品多的排前面；同样多时前缀长的更具体，排前面
    out.sort(key=lambda e: (-e.count, -len(e.prefix)))
    return out[:limit]


# ------------------------------------------------------------------ 原位测量的文件夹名
_RUN_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{0,2})$")


@dataclass(frozen=True)
class RunFolder:
    """一次原位测量的文件夹名解析结果。

        ZG0013_2026072918354709_Mode5_202607291932_SPS100
        └ device ┘└─ measured_at ─┘└mode┘└ 次要时间戳 ┘└ 其余 ┘

    **sample 用完整文件夹名**，不是 device。同一片样品可能测好几次
    （ZG0014 就有两个文件夹），只按 device 认身份会把它们静默合并。
    device 单独留一个字段，用来做「样品号」这一维筛选。
    """
    name: str                      # 完整文件夹名 = 样品名
    device: str = ""               # ZG0013
    measured_at: str = ""          # ISO 8601，解析不出来就是空
    mode: str = ""                 # Mode5
    extras: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "device": self.device,
                "measured_at": self.measured_at, "mode": self.mode,
                "extras": list(self.extras)}


def parse_run_folder(folder_name: str) -> RunFolder:
    """从文件夹名里挖出样品号、测量时间和模式。

    挖不出来也不报错 —— 文件夹名本身永远是样品名，其余字段留空。
    命名规则以后变了，最坏情况是筛选少一维，不会导不进来。
    """
    name = folder_name.strip().rstrip("/")
    parts = [p for p in name.split("_") if p]
    if not parts:
        return RunFolder(name=name)

    device = parts[0]
    measured_at = ""
    mode = ""
    extras: list[str] = []

    for seg in parts[1:]:
        m = _RUN_TS.match(seg)
        if m and not measured_at:
            y, mo, d, hh, mm, ss, frac = m.groups()
            measured_at = f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"
            if frac:
                measured_at += f".{frac.ljust(2, '0')}"
            continue
        if seg.lower().startswith("mode") and not mode:
            mode = seg
            continue
        extras.append(seg)

    return RunFolder(name=name, device=device, measured_at=measured_at,
                     mode=mode, extras=tuple(extras))
