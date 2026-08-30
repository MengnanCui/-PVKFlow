"""算子集 —— 模块能用的那些「便宜到可以边拖边算」的运算。

## 为什么要有这个东西

模块的算法跑在后端。可是「拖着波段滑块看曲线连续变化」这件事经不起
一次网络往返：实测浏览器本地算是 **2.2 ms**，往返至少 20–50 ms，
差 10–25 倍，手感完全不是一回事。

但也不能让模块作者去写 JS —— 那样风格和正确性就都失控了。

出路是：**把这类运算收进平台，做成封闭的算子集，一个算子两份实现**
（Python 一份给批处理，JS 一份给拖动），并用测试钉住两份数值相同。
模块只是**引用**算子，不实现它。于是模块作者白拿了「能拖」这个能力，
一行 JS 都不用写。

这个模式其实早就在跑了 —— `app/parsers/render.py` 和 `web/js/spectra.js`
里那两个函数就是一对双实现，连注释里的理由都是同一句话。
这里只是给它一个名字、封闭成集合、加上一致性测试。

## 加一个算子的代价

要同时写三样：Python 实现、JS 实现（`web/js/ops.js`）、一致性测试。
**所以算子集是平台维护的，模块作者不能自己加。** 这是有意的：
一个算子进了平台就人人都能用，值得走一遍这个流程。
算子拼不出来的，模块写普通 Python（B 档），松手才算 —— 那也正是
平台现在对贵操作（膜厚 FFT）的做法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

import numpy as np

from app.parsers import render

# 算子参数从哪种控件取值。校验器用它来检查模块的绑定对不对。
ArgKind = Literal["band", "number"]


@dataclass(frozen=True)
class OpArg:
    name: str
    kind: ArgKind
    label: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "label": self.label or self.name}


@dataclass(frozen=True)
class OpDef:
    """一个算子。`fn` 是 Python 那一份，`js` 是 web/js/ops.js 里对应的名字。"""
    name: str
    label: str
    args: tuple[OpArg, ...]
    fn: Callable[..., np.ndarray]
    unit: str = ""
    y_label: str = ""
    help: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "unit": self.unit,
                "y_label": self.y_label, "help": self.help,
                "args": [a.as_dict() for a in self.args]}


def _band_integral(M, lam, *, band) -> np.ndarray:
    lo, hi = float(band[0]), float(band[1])
    return render.band_integral(M, lam, lo, hi)


def _wavelength_slope(M, lam, *, center, half) -> np.ndarray:
    return render.wavelength_slope(M, lam, float(center), float(half))


# 封闭的算子集。**加算子请连 web/js/ops.js 和 tests/test_ops.py 一起改** ——
# 只加一边的话，界面上拖出来的数和存进库里的数会不一样，而且没有任何提示。
OPS: dict[str, OpDef] = {
    op.name: op for op in (
        OpDef(
            name="band_integral",
            label="波段积分",
            args=(OpArg("band", "band", "波段"),),
            fn=_band_integral,
            unit="a.u.·nm",
            y_label="积分强度 (a.u.·nm)",
            help="把指定波段内的强度对波长积分，随时间画出来。梯形法，"
                 "所以拖动波段边界时曲线连续、不会因为多算少算一个采样点而跳变。",
        ),
        OpDef(
            name="wavelength_slope",
            label="谱斜率",
            args=(OpArg("center", "number", "波长"), OpArg("half", "number", "半宽")),
            fn=_wavelength_slope,
            unit="a.u./nm",
            y_label="dI/dλ (a.u./nm)",
            help="在指定波长 ±半宽的窗口里对光谱做最小二乘线性拟合，"
                 "取斜率随时间画出来。比两点差分抗噪。",
        ),
    )
}


def get(name: str) -> OpDef:
    if name not in OPS:
        raise KeyError(
            f"没有这个算子：{name}。现有的：{', '.join(sorted(OPS))}。"
            "算子集是平台维护的封闭集合 —— 需要新算子请找平台维护者，"
            "或者把这个面板改成自定义 Python（去掉 live=，写 compute()）。")
    return OPS[name]


def run(name: str, M: np.ndarray, lam: np.ndarray, args: dict[str, Any]) -> np.ndarray:
    """按名字跑一个算子。参数已经由调用方从控件值解出来了。"""
    op = get(name)
    missing = [a.name for a in op.args if a.name not in args]
    if missing:
        raise ValueError(f"算子 {name} 缺参数：{', '.join(missing)}")
    return op.fn(M, lam, **{a.name: args[a.name] for a in op.args})


def catalog() -> list[dict]:
    """给界面和文档看的算子清单。"""
    return [OPS[k].as_dict() for k in sorted(OPS)]
