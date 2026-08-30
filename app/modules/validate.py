"""模块验证器 —— 这是给同事（和他的模型）的反馈通道。

装一个模块之前先跑这个。不通过就装不上。

**每一条报错都必须做到三件事**：点名是哪个字段、说清错在哪、给出合法值是什么。
做不到这三件事的报错等于没有报错 —— 模型看不懂就只能瞎猜，
「报错 → 改 → 再试」这个循环也就收敛不了。

反面教材：`ValueError: invalid panel`
正面教材：

    ✗ panel "integ" 的 uses 里写了 "integral"，但 controls 里没有这个 key。
      现有的 controls：slope_center, slope_half, integ
      你是不是想写 "integ"？

这跟平台里其它地方的原则是同一条 —— **错误信息是诊断通道** ——
只不过这一次读错误的是模型，不是人。
"""
from __future__ import annotations

import difflib
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app import config
from app.modules import ops
from app.modules.base import Module, ModuleContext, ModuleSpec, PanelData

ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


@dataclass
class Report:
    module_id: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "module_id": self.module_id, "errors": self.errors,
                "warnings": self.warnings, "checked": self.checked}


def _did_you_mean(wrong: str, options) -> str:
    """打错一个字母是最常见的错。直接把最像的那个说出来，省一轮往返。"""
    near = difflib.get_close_matches(wrong, list(options), n=1, cutoff=0.6)
    return f"　你是不是想写 \"{near[0]}\"？" if near else ""


def _glossary_terms() -> set[str]:
    """从 web/js/glossary.js 里读出术语 id。

    术语表是界面文案，只该有一份 —— 所以这里读它，不在 Python 里另存一份。
    """
    p = config.ROOT / "web" / "js" / "glossary.js"
    if not p.is_file():
        return set()
    try:
        body = p.read_text(encoding="utf-8").split("export const TERMS = {", 1)[1]
    except (IndexError, OSError):
        return set()
    return set(re.findall(r"^  '?([\w:]+)'?:\s*\{", body, re.M))


# ------------------------------------------------------------------ 声明检查
def check_spec(spec: Any, *, known_ids: set[str] | None = None) -> Report:
    r = Report()
    if not isinstance(spec, ModuleSpec):
        r.err(f"spec 必须是 ModuleSpec 实例，现在是 {type(spec).__name__}。"
              "写法：`spec = ModuleSpec(id=..., name=..., version=...)`")
        return r

    r.module_id = spec.id
    r.checked.append("声明结构")

    # ── id
    if not ID_RE.match(spec.id or ""):
        r.err(f"id \"{spec.id}\" 不合法。要求：小写字母开头、至少带一个点分段，"
              "比如 \"pl.peak\" 或 \"thickness.profilometer\"。"
              "点号前面是大类，后面是具体做法。")
    if known_ids and spec.id in known_ids:
        r.err(f"id \"{spec.id}\" 和已经装着的模块重名。改个 id，"
              "或者先把那个卸载掉。")
    if not (spec.name or "").strip():
        r.err("name 不能为空 —— 它是界面上显示的名字。")
    if not re.match(r"^\d+\.\d+\.\d+$", spec.version or ""):
        r.err(f"version \"{spec.version}\" 不合法，要写成 x.y.z（比如 \"1.0.0\"）。"
              "它会跟着结果一起落库，是结果可追溯的一部分。")

    # ── 控件
    r.checked.append("控件声明")
    keys: set[str] = set()
    for c in spec.controls:
        if c.key in keys:
            r.err(f"控件 key \"{c.key}\" 重复了。每个控件的 key 必须唯一。")
        keys.add(c.key)
        if not re.match(r"^[a-z][a-z0-9_]*$", c.key or ""):
            r.err(f"控件 key \"{c.key}\" 不合法：只能用小写字母、数字和下划线，字母开头。")
        if c.type == "band":
            d = c.default
            if not (isinstance(d, (list, tuple)) and len(d) == 2):
                r.err(f"控件 \"{c.key}\" 是 band 类型，default 要给两个数（[起, 止]），"
                      f"现在是 {d!r}。")
            elif d[0] >= d[1]:
                r.err(f"控件 \"{c.key}\" 的 default 是 [{d[0]}, {d[1]}]，起点不小于终点。")
        elif c.type == "select" and not c.options:
            r.err(f"控件 \"{c.key}\" 是 select 类型，但没给 options。")
        elif c.type == "number" and c.default is None:
            r.warn(f"控件 \"{c.key}\" 没有 default。界面上会是空的，建议给一个。")

    # ── 面板
    r.checked.append("面板声明")
    terms = _glossary_terms()
    panel_ids: set[str] = set()
    if not spec.panels:
        r.err("一个面板都没有。模块至少要有一格图，否则界面上什么都不会出现。")
    for p in spec.panels:
        if p.id in panel_ids:
            r.err(f"面板 id \"{p.id}\" 重复了。")
        panel_ids.add(p.id)
        if not (p.title or "").strip():
            r.err(f"面板 \"{p.id}\" 没有 title。")

        for u in p.uses:
            if u not in keys:
                r.err(f"panel \"{p.id}\" 的 uses 里写了 \"{u}\"，但 controls 里没有这个 key。"
                      f"　现有的 controls：{', '.join(sorted(keys)) or '（一个都没有）'}"
                      + _did_you_mean(u, keys))

        if p.info and terms and p.info not in terms:
            r.err(f"panel \"{p.id}\" 的 info=\"{p.info}\" 在术语表里不存在。"
                  "　要么把这条术语加进 web/js/glossary.js，要么把 info 去掉。"
                  + _did_you_mean(p.info, terms))

        # ── 算子绑定：A 档的关键检查
        if p.live:
            try:
                od = ops.get(p.live.op)
            except KeyError as exc:
                r.err(f"panel \"{p.id}\"：{exc}")
                continue
            for arg_name, ctrl_key in p.live.bind.items():
                arg = next((a for a in od.args if a.name == arg_name), None)
                if arg is None:      # Op.xxx() 已经拦过，这里兜底
                    r.err(f"panel \"{p.id}\"：算子 {p.live.op} 没有参数 \"{arg_name}\"。")
                    continue
                ctrl = spec.control(ctrl_key)
                if ctrl is None:
                    r.err(f"panel \"{p.id}\" 把算子参数 {arg_name} 绑到了控件 \"{ctrl_key}\"，"
                          f"但没有这个控件。"
                          f"　现有的 controls：{', '.join(sorted(keys)) or '（一个都没有）'}"
                          + _did_you_mean(ctrl_key, keys))
                elif ctrl.type != arg.kind:
                    r.err(f"panel \"{p.id}\"：算子 {p.live.op} 的参数 {arg_name} "
                          f"要一个 {arg.kind} 类型的控件，但 \"{ctrl_key}\" 是 "
                          f"{ctrl.type} 类型。"
                          f"　合法的 {arg.kind} 控件："
                          f"{', '.join(sorted(c.key for c in spec.controls if c.type == arg.kind)) or '（一个都没有，请先加一个）'}")
                elif ctrl_key not in p.uses:
                    # 不是错，但控件会画到别的格子上，多半不是本意
                    r.warn(f"panel \"{p.id}\" 用到了控件 \"{ctrl_key}\"，"
                           f"但它不在这个面板的 uses 里 —— 控件会画在别的格子上面。"
                           f"　建议把 \"{ctrl_key}\" 加进 uses。")

    # ── 批处理声明
    r.checked.append("批处理声明")
    for cv in spec.batch_curves:
        if cv.from_panel not in panel_ids:
            r.err(f"batch_curves 里的 \"{cv.name}\" 说数据来自面板 \"{cv.from_panel}\"，"
                  f"但没有这个面板。"
                  f"　现有的面板：{', '.join(sorted(panel_ids)) or '（一个都没有）'}"
                  + _did_you_mean(cv.from_panel, panel_ids))
        if not re.match(r"^[a-z][a-z0-9_]*$", cv.name or ""):
            r.err(f"batch_curves 里的列名 \"{cv.name}\" 不合法："
                  "只能用小写字母、数字和下划线（它要当数据表的列名）。")
    return r


# ------------------------------------------------------------------ 试跑
def trial_run(mod: Module, report: Report, *,
              lam=None, M=None, t=None) -> dict[str, PanelData] | None:
    """拿一份数据真跑一遍。声明对了不代表算得出来。"""
    report.checked.append("试跑")
    spec = mod.spec

    if lam is None:
        # 合成一份带干涉条纹的谱：真实数据的形状，不是全零
        # （全零会让「返回了全 NaN」这条检查失效）
        lam = np.linspace(600.0, 1100.0, 160)
        t = np.linspace(0.0, 10.0, 30)
        ot = 3000 * (1 - 0.4 * t / 10)
        M = 0.6 + 0.2 * np.cos(2 * np.pi * 2 * ot[None, :] / lam[:, None])

    before = _snapshot_workspace()
    try:
        out = mod.compute(ModuleContext(lam, M, t, spec.defaults()))
    except Exception as exc:
        report.err(f"compute() 跑崩了：{type(exc).__name__}: {exc}\n"
                   + traceback.format_exc()[-1200:])
        return None

    if not isinstance(out, dict):
        report.err(f"compute() 要返回 dict（{{面板 id: PanelData}}），"
                   f"现在返回的是 {type(out).__name__}。")
        return None

    for p in spec.panels:
        if p.id not in out:
            report.err(f"声明了面板 \"{p.id}\"，但 compute() 没有返回它。"
                       f"　返回的是：{', '.join(sorted(out)) or '（空的）'}"
                       + ("　A 档面板（有 live=）由基类负责，"
                          "如果你重写了 compute()，记得先调 super().compute(ctx)。"
                          if p.live else ""))
            continue
        d = out[p.id]
        if not isinstance(d, PanelData):
            report.err(f"面板 \"{p.id}\" 返回的是 {type(d).__name__}，要 PanelData。")
            continue
        if len(d.x) != len(d.y):
            report.err(f"面板 \"{p.id}\"：x 有 {len(d.x)} 个点，y 有 {len(d.y)} 个，对不上。")
        elif len(d.y) != len(t):
            report.err(f"面板 \"{p.id}\" 返回了 {len(d.y)} 个点，"
                       f"但时间轴有 {len(t)} 帧。曲线要和时间轴一一对应。")
        vals = [v for v in d.y if v is not None and v == v]
        if not vals:
            report.err(f"面板 \"{p.id}\" 算出来全是空值。"
                       "多半是控件默认值落在数据范围外了 —— "
                       f"这份试跑数据的波长范围是 {lam[0]:.0f}–{lam[-1]:.0f} nm。")

    extra = set(out) - {p.id for p in spec.panels}
    if extra:
        report.warn(f"compute() 返回了没有声明的面板：{', '.join(sorted(extra))}。"
                    "界面上不会显示它们 —— 要显示就加进 panels。")

    changed = _changed_outside(before, config.MODULES_DIR)
    if changed:
        report.err("模块在自己的目录之外写了文件："
                   + "、".join(str(p) for p in changed[:5])
                   + "。模块只应该读数据、返回结果，不该往工作区里写东西。")
    return out


def _snapshot_workspace() -> dict[Path, float]:
    """粗略快照。只抓工作区里的文件，够发现「不小心写了东西」。

    **这不是安全边界** —— 模块是 Python，能跑任意代码。这一条挡的是失误，
    不是恶意。装谁的模块就是信任谁，文档里也是这么写的。
    """
    out: dict[Path, float] = {}
    for d in (config.RAW_DIR, config.TABLES_DIR, config.DERIVED_DIR, config.CONFIG_DIR):
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    out[f] = f.stat().st_mtime
                except OSError:
                    pass
    return out


def _changed_outside(before: dict[Path, float], allowed: Path) -> list[Path]:
    after = _snapshot_workspace()
    changed = [p for p, m in after.items()
               if (p not in before or before[p] != m) and allowed not in p.parents]
    return changed


# ------------------------------------------------------------------ 总入口
def validate(mod: Module, *, known_ids: set[str] | None = None) -> Report:
    r = check_spec(getattr(mod, "spec", None), known_ids=known_ids)
    if r.ok:
        trial_run(mod, r)
    return r
