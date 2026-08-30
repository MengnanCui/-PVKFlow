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

import difflib
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import numpy as np

from app.modules import ops

# 控件类型。**故意只有这几种** —— 每多一种，平台就要多写一套渲染和校验，
# 而模块作者多一个选错的机会。不够用了再加，不预先铺开。
ControlType = Literal["number", "band", "select", "bool"]

# 面板画成什么。**故意只有三种** —— 平台自己最难的那个模块（膜厚）
# 也只用到这三种，够了就别再加。
#   xy      曲线图（默认）。一条或多条序列。
#   heatmap 服务端渲染的位图 + 前端矢量坐标轴。二维数据该当位图传，别塞进 json。
#   text    等宽文本。规范报告、诊断输出这种整段要看的东西。
PanelKind = Literal["xy", "heatmap", "text"]

# 序列的颜色**只收语义名**，不收十六进制。
#
# 一旦同事能写 `#ff00ff`，调色板就守不住了 —— 而「风格不会漂」正是
# 整套模块化的卖点。语义名还有一个好处：明暗主题自动跟着变，
# 写死的颜色在暗色主题下多半就瞎了。
SERIES_COLORS = {
    "auto": None,                  # 交给平台按序号配色
    "ok": "var(--ok)",
    "warn": "var(--warn)",
    "danger": "var(--danger)",
    "muted": "var(--ink-4)",
    "accent": "var(--accent)",
}


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
class Series:
    """曲线图里的一条序列。

    一个面板要画好几条时才用得上（比如膜厚那张：全部帧一条淡灰线打底、
    可信的绿散点、精度下降的琥珀散点压在上面）。只画一条的话，
    `PanelData(x=..., y=...)` 就够了，不用碰这个。
    """
    x: Sequence[float]
    y: Sequence[float | None]
    label: str = ""
    style: Literal["line", "scatter", "line+scatter"] = "line"
    color: str = "auto"            # 只能是 SERIES_COLORS 里的名字

    def as_dict(self) -> dict:
        return {"x": _nums(self.x), "y": _nums(self.y), "label": self.label,
                "style": self.style, "color": SERIES_COLORS.get(self.color)}


@dataclass(frozen=True)
class Stat:
    """图注里的一个数字。可以挂一个 ⓘ。

    膜厚图注里那串「可测下限 351 nm ⓘ　量化格距 29 nm ⓘ」就是这个。
    """
    label: str
    value: Any
    unit: str = ""
    info: str = ""                 # glossary.js 里的术语 id
    tone: Literal["", "ok", "warn", "danger"] = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "unit": self.unit,
                "info": self.info, "tone": self.tone}


@dataclass(frozen=True)
class Notice:
    """面板级的提示块。

    给「这一格是对照，不是测量结果」这种**必须看见**的说明用 ——
    塞进图注会被当成脚注忽略掉。
    """
    kind: Literal["info", "warn", "danger"] = "info"
    title: str = ""
    body: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "body": self.body}


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
    kind: PanelKind = "xy"
    y_label: str = ""
    x_label: str = "时间 (s)"
    info: str = ""                     # glossary.js 里的术语 id，会变成标题旁的 ⓘ
    caption: str = ""
    height: int = 300
    # 占几列。0 = 跟着模块的 columns 走；1 = 独占一整行（报告这种整幅宽的用）
    span: int = 0

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "uses": list(self.uses),
                "live": self.live.as_dict() if self.live else None,
                "kind": self.kind,
                "y_label": self.y_label or (ops.get(self.live.op).y_label if self.live else ""),
                "x_label": self.x_label, "info": self.info,
                "caption": self.caption, "height": self.height, "span": self.span}


@dataclass(frozen=True)
class Curve:
    """声明一条「随时间变化的曲线」要进批处理。

    声明了它，平台就会自动把它接进：批处理长表、对比页叠图、
    时刻切片对比、导出脚本。你不用碰 `app/batch.py`。
    """
    name: str                          # 长表里的列名
    from_panel: str                    # 数据从哪个面板来
    label: str = ""                    # 图上的 Y 轴标签，不填就用面板的
    # 不填 = 取那一格画出来的第一条序列；
    # 填了 = 从那一格的 `batch_extra[key]` 里取（不画在图上的数据）
    key: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "from_panel": self.from_panel,
                "label": self.label, "key": self.key}


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
    # 批处理时要不要拿**全部帧**算。
    #
    # 批处理有个 max_time_points，会把时间轴抽稀 —— 那是为了长表别太大，
    # 对逐帧的量（波段积分、谱斜率）无所谓：先抽再算和先算再抽结果一样。
    #
    # 但膜厚不一样：它是要看干燥过程哪一秒变坏的，抽稀等于把答案抹掉。
    # 声明了这个，平台就单独拿未抽稀的矩阵再跑一次这个模块，
    # 它的曲线存进另一张表（时间轴不同，硬塞进同一张就得给别人插值造数）。
    #
    # 代价是这个模块要多算一遍全部帧，所以**贵的才开**。
    batch_all_frames: bool = False
    description: str = ""
    author: str = ""
    origin: str = "builtin"            # builtin | user
    # 每格图占几列。2 = 一行两格（和样品页现有的三个模块一致）
    columns: int = 2
    # 页面上的先后。小的排前面。平台自带的用 10/20/…，
    # 同事的模块默认 100 排在后面 —— 他不该能把自己插到光谱和膜厚前面去。
    order: int = 100

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
            "batch_all_frames": self.batch_all_frames,
            "description": self.description, "author": self.author,
            "origin": self.origin, "order": self.order,
            # 界面据此决定拖动时能不能本地算
            "live_panels": [p.id for p in self.panels if p.live],
        }


# ------------------------------------------------------------------ 运行时
@dataclass
class PanelData:
    """一个面板的数据。B 档的 `compute()` 返回这个。

    **最简单的写法一个字没变**：`PanelData(x=t, y=y)`。
    下面那些字段全是可选的，要用才写：

        series      画多条曲线时用（给了它就不看 x/y）
        stats       图注里那串带 ⓘ 的数字
        notice      面板级的提示块
        info_extra  喂给标题旁 ⓘ 的附加段（跟着当前数据走的内容）
        image_url…  kind="heatmap" 的面板用
        text        kind="text" 的面板用
    """
    x: Sequence[float] = ()
    y: Sequence[float | None] = ()
    label: str = ""
    y_label: str = ""
    caption: str = ""
    # ── 以下都是可选的
    series: Sequence[Series] = ()
    stats: Sequence[Stat] = ()
    notice: Notice | None = None
    info_extra: dict | None = None
    # kind="heatmap"
    image_url: str = ""
    x_range: Sequence[float] | None = None
    y_range: Sequence[float] | None = None
    v_range: Sequence[float] | None = None
    v_label: str = ""
    cmap: str = ""
    # kind="text"
    text: str = ""
    # 这一格算出来、但**不画在图上**、又该进批处理的数字。
    #
    # 膜厚就需要它：每帧的判级（可信=1 / 不可信=0）不该画进曲线，
    # 但批处理要拿它算「这个时间窗里有几帧可信」。没有这个字段的话，
    # 批处理只能自己再跑一遍 FFT —— 那就是同一件事有两份实现，
    # 迟早对不上，而且没人知道该信哪个。
    batch_extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        # 只有一条序列时也统一成 series 交给前端 —— 前端就不用写两条渲染路径了。
        # 但**契约这一侧仍然允许只写 x/y**，那才是九成的情况。
        series = list(self.series) or (
            [Series(x=self.x, y=self.y, label=self.label)] if len(self.x) else [])
        return {
            "x": _nums(self.x), "y": _nums(self.y),
            "label": self.label, "y_label": self.y_label, "caption": self.caption,
            "series": [s.as_dict() for s in series],
            "stats": [s.as_dict() for s in self.stats],
            "notice": self.notice.as_dict() if self.notice else None,
            "info_extra": self.info_extra,
            "image_url": self.image_url,
            "x_range": list(self.x_range) if self.x_range else None,
            "y_range": list(self.y_range) if self.y_range else None,
            "v_range": list(self.v_range) if self.v_range else None,
            "v_label": self.v_label, "cmap": self.cmap,
            "text": self.text,
            "batch_extra": {k: _nums(v) for k, v in (self.batch_extra or {}).items()},
        }

    def column(self, key: str = "") -> list | None:
        """批处理要的那一列。给 `Curve` 用，模块作者不用调。

        不给 key = 这一格画出来的主序列（写了 series 就是第一条）；
        给了 key = `batch_extra` 里那一列（算出来但不画在图上的东西）。

        取不到返回 None，不是空列表 —— 「这一列压根没有」和「这一列全是空值」
        要报不同的话，混成一个的话批处理的 warning 就会指错方向。
        """
        if key:
            v = (self.batch_extra or {}).get(key)
            return None if v is None else list(v)
        if self.series:
            return list(self.series[0].y)
        return list(self.y) if len(self.y) else None

    @property
    def n_points(self) -> int:
        """这个面板有多少个点。多序列时取最长的那条 —— 校验器拿它和时间轴比。"""
        if self.series:
            return max((len(s.y) for s in self.series), default=0)
        return len(self.y)


def _nums(vals) -> list:
    """数值序列 → json 安全的列表。NaN / inf 变成 null（json 里没有 NaN 这个东西）。"""
    out = []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        f = float(v)
        out.append(None if (f != f or f in (float("inf"), float("-inf"))) else round(f, 6))
    return out


class ModuleContext:
    """模块运行时拿得到的一切。矩阵已经载入好、缓存好了。"""

    def __init__(self, lam: np.ndarray, M: np.ndarray, t: np.ndarray,
                 params: dict[str, Any], meta: dict | None = None,
                 artifact_id: str = "") -> None:
        self.lam = lam            # 波长轴，shape (n_lambda,)
        self.M = M                # 矩阵，shape (n_lambda, n_time)
        self.t = t                # 时间轴，shape (n_time,)
        self.params = dict(params)
        self.meta = dict(meta or {})
        # 这份数据的 artifact id。服务端渲染类的面板（热力图）要拿它拼图片地址 ——
        # 二维数据当位图传，不塞进 json。
        self.artifact_id = artifact_id
        # 这一次真正需要重算的面板。None = 全都要（第一次算，或者调用方没说）。
        # 见 needs()。
        self._needed: set[str] | None = None

    def needs(self, panel_id: str) -> bool:
        """这一次要不要重算这个面板。

        面板在声明里写了 `uses=[...]`，平台就知道它依赖哪些控件。
        只动了波段控件时，一个 `uses=[]` 的面板（比如「全波段对照」那格）
        结果不可能变 —— 但它照样会被重算一遍。

        实测代价：膜厚模块里那格全波段 FFT 要 50 ms，占了整次重算的四分之一，
        而且**每拖一次都白算一遍**。

        所以贵的活包一层：

            if ctx.needs("ot_full"):
                out["ot_full"] = 很贵的计算(...)

        不写也没关系 —— 只是慢一点，不会算错。这是优化，不是义务。
        """
        return self._needed is None or panel_id in self._needed

    def image_url(self, endpoint: str, **query) -> str:
        """拼一个服务端渲染接口的地址，给 kind="heatmap" 的面板用。

            ctx.image_url("heatmap.png", axis="wavenumber", norm="frame", cmap="gray")

        自己拼字符串也行，但走这个的话 artifact_id 不会忘、参数会被正确编码，
        **而且写错的取值当场就报出来**。

        为什么要当场报：色标写成 `cmap="turbo"` 的话，浏览器那边只会显示
        一个红框「无法渲染这张图（HTTP 422）」—— 图没了，可为什么没了、
        能写哪几个，一个字都没有。而验证器会真跑一遍 `compute()`，
        所以在这里抛出来，就变成装模块之前看得见的一条明确报错。
        """
        from urllib.parse import urlencode
        if not self.artifact_id:
            return ""
        _check_image_query(endpoint, query)
        q = urlencode({k: v for k, v in query.items() if v is not None})
        return f"/api/spectra/{self.artifact_id}/{endpoint}" + (f"?{q}" if q else "")

    def param(self, key: str, default: Any = None) -> Any:
        v = self.params.get(key, default)
        return default if v is None else v

    def op(self, name: str, **args) -> np.ndarray:
        """在 `compute()` 里也能调平台算子 —— B 档面板照样可以复用它们。"""
        return ops.run(name, self.M, self.lam, args)


# 服务端渲染接口的取值白名单。**从 render.py 取，不在这儿抄第二份** ——
# 抄了之后加一个色标就得记着改两处，漏改的那处只会在运行时 422。
def _image_allowed() -> dict[str, dict[str, tuple[str, ...]]]:
    from app.parsers import render
    return {"heatmap.png": {"axis": render.AXES, "norm": render.NORMS,
                            "cmap": tuple(render.COLORMAPS)}}


def _check_image_query(endpoint: str, query: dict) -> None:
    allowed = _image_allowed()
    if endpoint not in allowed:
        raise ValueError(
            f"image_url() 不认识这个接口：{endpoint!r}。"
            f"能用的是：{', '.join(sorted(allowed))}")
    for key, legal in allowed[endpoint].items():
        v = query.get(key)
        if v is None or str(v) in legal:
            continue
        near = difflib.get_close_matches(str(v), legal, n=1, cutoff=0.5)
        raise ValueError(
            f"image_url(\"{endpoint}\", {key}={v!r}) 里的 {key} 不认识。"
            f"能用的是：{', '.join(legal)}。"
            + (f"你是不是想写 {near[0]!r}？" if near else
               "这几个是平台的配色/坐标轴档位，加不了 —— "
               "换一个能表达同样意思的。"))


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
