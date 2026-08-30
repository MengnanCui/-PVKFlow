"""功能模块契约 —— 同事往平台里加功能，只需要写这一个文件。

和 `app/skills/base.py` 的分工：

* **Skill** 是「一次性处理」：文件进 → 结果出。适合「算一个数」。
* **Module** 是「交互式面板」：矩阵已经载入好了，参数一动就重算一条曲线。
  你在样品页看到的光谱处理 / 膜厚处理 / 特殊处理，就是这个形状。

一个模块 = `workspace/modules/<名字>/module.py` 一个文件。
放进去、点一下重载就生效，不用重启、不用 Git、不用分支。
发给别人就是发这个文件夹（或它的 zip）。

## 你写什么，平台写什么

你写：**声明**（有哪些控件、哪些面板、产出什么），加上（可选的）算法。
平台写：面板结构、控件样式、图注、下载菜单、ⓘ、左右对齐、批处理接入。

**你不写 HTML、不写 CSS、不写 JS。** 这不是为了省事，是为了让风格
不可能漂 —— 你根本没有画界面的机会，界面永远是平台按你的声明渲染的。

## 两档面板

**A 档（能拖）**：面板用平台的**算子**拼出来，写在 `live=`。
拖控件时算子在浏览器里跑，零延迟（实测 2.2 ms）。
算子集是封闭的（见 `app/modules/ops.py`），你不能自己加 —— 因为一个算子
要同时有 JS 和 Python 两份实现加一致性测试，那是平台维护者的活。

**B 档（松手才算）**：算子拼不出来的（峰拟合、FFT、任意 numpy），
写 `compute()`，跑在后端。参数松手才重算。

这不是降级 —— 平台自己的膜厚模块就是 B 档：FFT 本来就贵，
拖着算既没必要也做不到。**便宜的能拖，贵的松手才算**，就这么分。

## 最小例子

    from app.modules.base import Module, ModuleSpec, Control, Panel, Curve, Op

    class MyModule(Module):
        spec = ModuleSpec(
            id="pl.peak",
            name="荧光峰位",
            version="1.0.0",
            controls=[Control("band", "波段", "band", default=[700, 800], unit="nm")],
            panels=[Panel("integ", "荧光强度 vs 时间", uses=["band"],
                          live=Op.band_integral(band="band"))],
            batch_curves=[Curve("pl_integral", from_panel="integ")],
        )

    MODULE = MyModule()

完整说明见 docs/MODULE_AUTHORING.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import numpy as np

from app.modules import ops

# 控件类型。**故意只有这几种** —— 每多一种，平台就要多写一套渲染和校验，
# 而模块作者多一个选错的机会。不够用了再加，不预先铺开。
ControlType = Literal["number", "band", "select", "bool"]


# ------------------------------------------------------------------ 声明
@dataclass(frozen=True)
class Control:
    """一个控件。平台渲染成功能块里的输入框/滑块，样式不归你管。"""
    key: str
    label: str
    type: ControlType = "number"
    default: Any = None
    unit: str = ""
    options: Sequence[Any] = ()        # select 用
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""
    # 上下限跟着数据走，而不是写死。`"lambda"` = 跟着这份数据的波长范围。
    #
    # 为什么需要它：一个「波长」控件写死 max=100000 的话，滑块拖起来毫无意义
    # （950 在 1..100000 上几乎贴着最左边）；写死 max=1120 又只对你这台
    # 光谱仪成立，换一台就不对了。声明「跟着波长轴」才是对的那个意思。
    range_from: Literal["", "lambda", "time"] = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": self.default, "unit": self.unit, "options": list(self.options),
                "min": self.min, "max": self.max, "step": self.step, "help": self.help,
                "range_from": self.range_from}


@dataclass(frozen=True)
class OpRef:
    """对一个平台算子的引用，以及「算子的哪个参数从哪个控件取值」。

    `Op.band_integral(band="integ")` 就是造这个东西 ——
    算子叫 band_integral，它的 `band` 参数从 key 为 `integ` 的控件取值。
    """
    op: str
    bind: dict[str, str]               # 算子参数名 → 控件 key

    def as_dict(self) -> dict:
        return {"op": self.op, "bind": dict(self.bind)}


class _OpNamespace:
    """`Op.band_integral(band="integ")` 的实现。

    名字打错了在**模块定义的那一刻**就报错，而且会把现有算子列出来 ——
    不用等装到平台里才发现。
    """

    def __getattr__(self, name: str):
        op = ops.get(name)             # 不存在时抛的错里已经列出了所有算子
        valid = {a.name for a in op.args}

        def make(**bind: str) -> OpRef:
            unknown = set(bind) - valid
            if unknown:
                raise TypeError(
                    f"算子 {name} 没有参数 {', '.join(sorted(unknown))}。"
                    f"它的参数是：{', '.join(a.name + '（' + a.kind + '）' for a in op.args)}")
            missing = valid - set(bind)
            if missing:
                raise TypeError(
                    f"算子 {name} 还缺参数 {', '.join(sorted(missing))}。"
                    f"每个参数都要指定它从哪个控件取值，比如 "
                    f"{name}({list(valid)[0]}=\"某个控件的 key\")")
            return OpRef(op=name, bind=dict(bind))

        return make


Op = _OpNamespace()


@dataclass(frozen=True)
class Panel:
    """一格图。左右并排、三行结构、等高对齐都由平台保证。

    `live` 给了就是 A 档（拖控件时在浏览器里实时算）；
    没给就落到模块的 `compute()`（B 档，松手才算）。
    """
    id: str
    title: str
    uses: Sequence[str] = ()           # 这一格用到哪些控件（决定控件画在哪一格上面）
    live: OpRef | None = None
    y_label: str = ""
    x_label: str = "时间 (s)"
    info: str = ""                     # glossary.js 里的术语 id，会变成标题旁的 ⓘ
    caption: str = ""
    height: int = 300

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "uses": list(self.uses),
                "live": self.live.as_dict() if self.live else None,
                "y_label": self.y_label or (ops.get(self.live.op).y_label if self.live else ""),
                "x_label": self.x_label, "info": self.info,
                "caption": self.caption, "height": self.height}


@dataclass(frozen=True)
class Curve:
    """声明一条「随时间变化的曲线」要进批处理。

    声明了它，平台就会自动把它接进：批处理长表、对比页叠图、
    时刻切片对比、导出脚本。你不用碰 `app/batch.py`。
    """
    name: str                          # 长表里的列名
    from_panel: str                    # 数据从哪个面板来
    label: str = ""                    # 图上的 Y 轴标签，不填就用面板的

    def as_dict(self) -> dict:
        return {"name": self.name, "from_panel": self.from_panel, "label": self.label}


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name: str
    version: str
    controls: Sequence[Control] = ()
    panels: Sequence[Panel] = ()
    # 需要什么输入。目前只有一种：已经载入好的光谱矩阵。
    needs: Literal["spectra_matrix"] = "spectra_matrix"
    batch_curves: Sequence[Curve] = ()
    batch_metrics: Sequence[str] = ()
    description: str = ""
    author: str = ""
    origin: str = "builtin"            # builtin | user
    # 每格图占几列。2 = 一行两格（和样品页现有的三个模块一致）
    columns: int = 2

    def defaults(self) -> dict[str, Any]:
        return {c.key: c.default for c in self.controls}

    def control(self, key: str) -> Control | None:
        return next((c for c in self.controls if c.key == key), None)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "needs": self.needs, "columns": self.columns,
            "controls": [c.as_dict() for c in self.controls],
            "panels": [p.as_dict() for p in self.panels],
            "batch_curves": [c.as_dict() for c in self.batch_curves],
            "batch_metrics": list(self.batch_metrics),
            "description": self.description, "author": self.author,
            "origin": self.origin,
            # 界面据此决定拖动时能不能本地算
            "live_panels": [p.id for p in self.panels if p.live],
        }


# ------------------------------------------------------------------ 运行时
@dataclass
class PanelData:
    """一个面板的数据。B 档的 `compute()` 返回这个。"""
    x: Sequence[float]
    y: Sequence[float | None]
    label: str = ""
    y_label: str = ""
    caption: str = ""

    def as_dict(self) -> dict:
        def clean(v):
            if v is None:
                return None
            f = float(v)
            return None if f != f else round(f, 6)          # NaN → null
        return {"x": [None if v is None else round(float(v), 4) for v in self.x],
                "y": [clean(v) for v in self.y],
                "label": self.label, "y_label": self.y_label, "caption": self.caption}


class ModuleContext:
    """模块运行时拿得到的一切。矩阵已经载入好、缓存好了。"""

    def __init__(self, lam: np.ndarray, M: np.ndarray, t: np.ndarray,
                 params: dict[str, Any], meta: dict | None = None) -> None:
        self.lam = lam            # 波长轴，shape (n_lambda,)
        self.M = M                # 矩阵，shape (n_lambda, n_time)
        self.t = t                # 时间轴，shape (n_time,)
        self.params = dict(params)
        self.meta = dict(meta or {})

    def param(self, key: str, default: Any = None) -> Any:
        v = self.params.get(key, default)
        return default if v is None else v

    def op(self, name: str, **args) -> np.ndarray:
        """在 `compute()` 里也能调平台算子 —— B 档面板照样可以复用它们。"""
        return ops.run(name, self.M, self.lam, args)


class Module:
    """所有模块的基类。子类给一个 `spec`；纯 A 档的模块连 `compute` 都不用写。"""

    spec: ModuleSpec

    def compute(self, ctx: ModuleContext) -> dict[str, PanelData]:
        """B 档面板在这里算。返回 {panel_id: PanelData}。

        默认实现负责 A 档：把声明的算子跑一遍。所以子类如果全是 A 档面板，
        什么都不用重写；如果有 B 档面板，重写这个方法并**先调 super()**
        把 A 档那些拿到手，再补上自己的。
        """
        out: dict[str, PanelData] = {}
        for p in self.spec.panels:
            if not p.live:
                continue
            args = _resolve_bind(self.spec, p.live, ctx.params)
            y = ops.run(p.live.op, ctx.M, ctx.lam, args)
            out[p.id] = PanelData(
                x=list(ctx.t), y=list(y), label=p.title,
                y_label=p.as_dict()["y_label"], caption=p.caption)
        return out


def _resolve_bind(spec: ModuleSpec, ref: OpRef, params: dict[str, Any]) -> dict[str, Any]:
    """把 `{算子参数: 控件 key}` 解成 `{算子参数: 实际值}`。"""
    args: dict[str, Any] = {}
    for arg_name, ctrl_key in ref.bind.items():
        if ctrl_key not in params:
            c = spec.control(ctrl_key)
            args[arg_name] = c.default if c else None
        else:
            args[arg_name] = params[ctrl_key]
    return args
