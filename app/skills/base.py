"""Skill 契约 —— 这个平台最重要的一份接口。

一个 skill 就是「一种处理能力」：膜厚、光谱、J-V、图像分析……
平台只负责四件事：把文件递给你、把参数递给你、把你返回的结果存好、把结果画出来。
算法本身永远在 skill 里，不在平台里。

写一个 skill 最少需要什么：

    from app.skills.base import Skill, SkillSpec, ParamSpec, OutputSpec, SkillResult, Metric

    class MySkill(Skill):
        spec = SkillSpec(
            id="thickness.profilometer",
            name="台阶仪膜厚",
            category="thickness",
            version="1.0.0",
            accepts=FileMatch(extensions=[".csv", ".txt"]),
            params=[ParamSpec("baseline", "基线区间", "range", default=[0, 100], unit="μm")],
            outputs=[OutputSpec("thickness", "膜厚", unit="nm")],
        )

        def run(self, ctx):
            df, _ = ctx.load_table()             # 平台已经帮你嗅探好编码/分隔符/表头
            value = my_algorithm(df, ctx.params["baseline"])
            return SkillResult(
                metrics=[Metric("thickness", value, unit="nm")],
                tables={"profile": df},
                preview=ChartSpec.line(df, x="position", y="height"),
            )

`params` 会自动渲染成界面上的表单，`outputs` 会自动渲染成结果卡片，
`preview` 会自动渲染成图表。你不需要碰任何前端代码。

完整说明见 docs/SKILL_CONTRACT.md。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

ParamType = Literal["number", "text", "select", "bool", "range", "column", "columns"]
Category = str  # thickness | spectrum | jv | image | table | ...


# ------------------------------------------------------------------ 声明
@dataclass(frozen=True)
class ParamSpec:
    """一个参数。界面据此生成表单控件，不需要写前端。"""
    key: str
    label: str
    type: ParamType = "number"
    default: Any = None
    options: Sequence[Any] = ()      # select 用
    unit: str = ""
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    required: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "type": self.type,
            "default": self.default, "options": list(self.options), "unit": self.unit,
            "help": self.help, "min": self.min, "max": self.max, "step": self.step,
            "required": self.required,
        }


@dataclass(frozen=True)
class OutputSpec:
    """声明会产出哪个关键结果字段。界面据此生成结果卡片。"""
    field_name: str
    label: str = ""
    unit: str = ""
    kind: Literal["number", "text"] = "number"
    help: str = ""

    def as_dict(self) -> dict:
        return {"field_name": self.field_name, "label": self.label or self.field_name,
                "unit": self.unit, "kind": self.kind, "help": self.help}


@dataclass(frozen=True)
class FileMatch:
    """这个 skill 吃什么文件。三个条件是「或」的关系。"""
    extensions: Sequence[str] = ()
    filename_globs: Sequence[str] = ()
    content_keywords: Sequence[str] = ()   # 命中文件抬头里的关键词（大小写不敏感）
    min_files: int = 1
    max_files: int | None = None

    def as_dict(self) -> dict:
        return {"extensions": list(self.extensions), "filename_globs": list(self.filename_globs),
                "content_keywords": list(self.content_keywords),
                "min_files": self.min_files, "max_files": self.max_files}


@dataclass(frozen=True)
class SkillSpec:
    id: str
    name: str
    category: Category
    version: str
    accepts: FileMatch = field(default_factory=FileMatch)
    params: Sequence[ParamSpec] = ()
    outputs: Sequence[OutputSpec] = ()
    description: str = ""
    author: str = ""
    origin: str = "builtin"        # builtin | user | skill.md
    ready: bool = True             # False 表示契约在但算法未接入，界面会诚实标注
    ready_note: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "version": self.version, "accepts": self.accepts.as_dict(),
            "params": [p.as_dict() for p in self.params],
            "outputs": [o.as_dict() for o in self.outputs],
            "description": self.description, "author": self.author,
            "origin": self.origin, "ready": self.ready, "ready_note": self.ready_note,
        }

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}


# ------------------------------------------------------------------ 输入
@dataclass
class FileRef:
    """递给 skill 的一个文件。path 一定是可以直接打开的绝对路径。"""
    artifact_id: str
    path: Path
    filename: str
    ext: str
    size: int = 0
    display_path: str = ""
    sample_id: str | None = None
    sample_name: str = ""
    mime: str | None = None


# ------------------------------------------------------------------ 输出
@dataclass
class Metric:
    """一个关键结果。数值或文本。"""
    field_name: str
    value: Any
    unit: str = ""
    label: str = ""
    quality: Literal["validated", "review", "reject"] = "review"
    source: Literal["raw", "manual", "skill", "ai"] = "skill"

    def as_dict(self) -> dict:
        return {"field_name": self.field_name, "value": self.value, "unit": self.unit,
                "label": self.label or self.field_name, "quality": self.quality,
                "source": self.source}


@dataclass
class Figure:
    """skill 自己画好的图（SVG / PNG 字节流）。"""
    name: str
    data: bytes
    mime: str = "image/svg+xml"


@dataclass
class Series:
    label: str
    x: list[float]
    y: list[float]
    style: Literal["line", "scatter", "line+scatter"] = "line"

    def as_dict(self) -> dict:
        return {"label": self.label, "x": self.x, "y": self.y, "style": self.style}


@dataclass
class ChartSpec:
    """交给前端画的图。前端有自己的 SVG 绘图模块，不需要 skill 生成图片。"""
    kind: Literal["xy", "bar", "none"] = "xy"
    x_label: str = ""
    y_label: str = ""
    series: list[Series] = field(default_factory=list)
    x_log: bool = False
    y_log: bool = False
    annotations: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "x_label": self.x_label, "y_label": self.y_label,
                "series": [s.as_dict() for s in self.series],
                "x_log": self.x_log, "y_log": self.y_log, "annotations": self.annotations}

    @staticmethod
    def from_frame(df, x: str, ys: Sequence[str], x_label: str = "", y_label: str = "",
                   style: str = "line", max_points: int = 4000) -> "ChartSpec":
        """DataFrame → 图。超过 max_points 时等间隔抽稀，保证界面不卡。"""
        import numpy as np

        n = len(df)
        step = max(1, n // max_points)
        sub = df.iloc[::step]
        xv = [None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
              for v in sub[x].tolist()]
        series = []
        for col in ys:
            if col not in sub.columns:
                continue
            yv = [None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
                  for v in sub[col].tolist()]
            series.append(Series(label=col, x=xv, y=yv, style=style))  # type: ignore[arg-type]
        # 只有一条序列时，把列名直接当 Y 轴标签——图例就没必要了
        if not y_label and len(series) == 1:
            y_label = series[0].label
        return ChartSpec(kind="xy", x_label=x_label or x, y_label=y_label, series=series)


@dataclass
class SkillResult:
    metrics: list[Metric] = field(default_factory=list)
    tables: dict[str, Any] = field(default_factory=dict)      # name -> DataFrame
    figures: list[Figure] = field(default_factory=list)
    preview: ChartSpec | None = None
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    logs: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 上下文
class SkillContext:
    """skill 运行时能拿到的一切。"""

    def __init__(
        self,
        files: Sequence[FileRef],
        params: dict[str, Any],
        run_id: str,
        tmp_dir: Path,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.files = list(files)
        self.params = dict(params)
        self.run_id = run_id
        self.tmp_dir = tmp_dir
        self._lines: list[str] = []
        self._log = log

    # -- 便利访问 --
    @property
    def file(self) -> FileRef:
        """单文件 skill 的快捷入口。"""
        if not self.files:
            raise ValueError("没有输入文件")
        return self.files[0]

    @property
    def path(self) -> Path:
        return self.file.path

    def param(self, key: str, default: Any = None) -> Any:
        v = self.params.get(key, default)
        return default if v is None else v

    def load_table(self, index: int = 0, max_rows: int | None = None):
        """把输入文件读成 DataFrame。编码/分隔符/抬头由平台嗅探好。

        返回 (DataFrame, Sniffed)。
        """
        from app.parsers import sniff

        ref = self.files[index]
        return sniff.load_frame(ref.path, max_rows=max_rows)

    def sniff(self, index: int = 0):
        from app.parsers import sniff as s

        return s.sniff_text(self.files[index].path)

    def load_image(self, index: int = 0):
        from PIL import Image

        return Image.open(self.files[index].path)

    def logline(self, msg: str) -> None:
        self._lines.append(str(msg))
        if self._log:
            self._log(str(msg))

    @property
    def log_text(self) -> str:
        return "\n".join(self._lines)


# ------------------------------------------------------------------ 基类
class Skill:
    """所有 skill 的基类。只有 `spec` 和 `run` 是必须的。"""

    spec: SkillSpec

    def can_handle(self, files: Sequence[FileRef]) -> float:
        """返回 0–1 的置信度，平台据此给用户排推荐顺序。

        默认实现按 spec.accepts 打分，绝大多数 skill 不需要重写。
        """
        return default_match_score(self.spec.accepts, files)

    def run(self, ctx: SkillContext) -> SkillResult:  # pragma: no cover - 抽象
        raise NotImplementedError(f"{type(self).__name__} 没有实现 run()")


def default_match_score(accepts: FileMatch, files: Sequence[FileRef]) -> float:
    """按扩展名 / 文件名 glob / 抬头关键词打分，0–1。

    打分的关键不只是"命中加分"，还有"声明了却没命中就减分"——
    一个声明了 `*spec*` 的光谱 skill 碰到 `B12_S1_jv.csv`，
    扩展名虽然对得上，但这恰恰是它不该被推荐的信号。
    没有这条，所有吃 .csv 的 skill 会挤在同一个分数上，排序等于没有。
    """
    if not files:
        return 0.0
    if len(files) < accepts.min_files:
        return 0.0
    if accepts.max_files is not None and len(files) > accepts.max_files:
        return 0.0

    exts = {e.lower() for e in accepts.extensions}
    globs = list(accepts.filename_globs)
    keywords = [k.lower() for k in accepts.content_keywords]

    scores: list[float] = []
    for f in files:
        if exts:
            if f.ext.lower() not in exts:
                scores.append(0.0)          # 扩展名不对就是不对
                continue
            score = 0.35
        else:
            score = 0.15                    # 不限扩展名的通用 skill，底分低一些

        if globs:
            if any(fnmatch.fnmatch(f.filename.lower(), g.lower()) for g in globs):
                score += 0.35
            else:
                score *= 0.6                # 声明了文件名特征却没命中
        if keywords:
            if _has_keyword(f.path, keywords):
                score += 0.30
            else:
                score *= 0.6                # 声明了内容特征却没命中
        scores.append(round(min(score, 1.0), 3))

    return round(sum(scores) / len(scores), 3)


def _has_keyword(path: Path, keywords: Sequence[str]) -> bool:
    """只读文件头 8 KB 找关键词——够用且不会因为大文件卡住。"""
    try:
        head = path.open("rb").read(8192).decode("utf-8", errors="ignore").lower()
    except OSError:
        return False
    return any(k in head for k in keywords)
