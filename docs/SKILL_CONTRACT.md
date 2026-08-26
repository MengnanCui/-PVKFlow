# Skill 契约

一个 skill 就是**一种处理能力**：膜厚、光谱、J-V、图像分析……

平台负责四件事：把文件递给你、把参数递给你、把你返回的结果存好、把结果画出来。
**算法永远在 skill 里，不在平台里。**

最重要的结果：你声明的参数会自动变成界面上的表单，你声明的输出会自动变成结果卡片，
你返回的曲线会自动画成图。**你不需要碰任何前端代码。**

---

## 三分钟接一个 skill

```
workspace/skills/
└── my_thickness/
    └── skill.py
```

```python
from app.skills.base import (
    Skill, SkillSpec, ParamSpec, OutputSpec, FileMatch,
    SkillResult, Metric, ChartSpec,
)


class MyThickness(Skill):
    spec = SkillSpec(
        id="thickness.profilometer",     # 全局唯一
        name="台阶仪膜厚",                 # 界面上显示的名字
        category="thickness",
        version="1.0.0",                 # 会写进 analysis_run，保证结果可追溯
        accepts=FileMatch(extensions=[".csv", ".txt"]),
        params=[
            ParamSpec("baseline", "基线区间", "range", default=[0, 10], unit="%"),
            ParamSpec("smooth", "平滑窗口", "number", default=5, min=0, step=1),
        ],
        outputs=[
            OutputSpec("thickness", "膜厚", unit="nm"),
            OutputSpec("roughness", "粗糙度", unit="nm"),
        ],
    )

    def run(self, ctx):
        df, _ = ctx.load_table()          # 编码 / 分隔符 / 仪器抬头，平台已经嗅探好
        value = my_algorithm(df, ctx.param("baseline"))

        return SkillResult(
            metrics=[Metric("thickness", value, unit="nm")],
            tables={"profile": df},                                   # → Parquet
            preview=ChartSpec.from_frame(df, x="Position", ys=["Height"]),
        )


SKILL = MyThickness()
```

放好文件后，在「数据处理」页点 **重载 Skill**，不用重启服务。

---

## 平台会自动做什么

| 你写的 | 界面上出现什么 |
|---|---|
| `spec.params` | 参数表单（数字框、下拉、开关、区间、列选择器） |
| `spec.outputs` | 结果卡片，带单位 |
| `spec.accepts` | 选中文件时这个 skill 出现在候选列表里，并按匹配度排序 |
| `SkillResult.metrics` | 写进 `key_result` 长表，带来源、质量、算法版本 |
| `SkillResult.tables` | 写成 Parquet，登记进 `data_table` |
| `SkillResult.preview` | 画成交互式 SVG 图（可框选放大、悬停读数） |
| `SkillResult.figures` | 你自己画好的图，存成派生文件 |
| `SkillResult.warnings` | 结果区的黄色提示条 |

---

## SkillSpec

| 字段 | 必需 | 说明 |
|---|---|---|
| `id` | ✓ | 全局唯一，建议 `类别.具体方法` |
| `name` | ✓ | 中文名，界面显示 |
| `category` | ✓ | `thickness` / `spectrum` / `jv` / `image` / `table` / 自定义 |
| `version` | ✓ | 改算法就改版本号。它会写进每条结果，让你能区分「这个数是旧算法算的」 |
| `accepts` | | `FileMatch(...)`，决定推荐排序 |
| `params` | | `ParamSpec` 列表 |
| `outputs` | | `OutputSpec` 列表 |
| `description` | | 一句话说明，界面上显示 |
| `ready` | | `False` 表示契约在但算法没接入，界面会诚实标注「待接入」并拒绝运行 |
| `ready_note` | | `ready=False` 时说明还缺什么 |

### FileMatch —— 决定你的 skill 什么时候被推荐

```python
FileMatch(
    extensions=[".csv", ".txt"],           # 扩展名不对，直接 0 分
    filename_globs=["*thick*", "*膜厚*"],   # 文件名特征
    content_keywords=["thickness", "step height"],  # 文件头 8KB 里的关键词
    max_files=1,                            # 单文件 skill
)
```

打分规则（0–1）：扩展名命中 0.35，文件名命中 +0.35，关键词命中 +0.30。
**声明了却没命中要扣分**（×0.6）—— 一个声明了 `*spec*` 的光谱 skill 碰到
`B12_S1_jv.csv`，扩展名虽然对得上，但这恰恰是它不该被推荐的信号。
没有这条惩罚，所有吃 `.csv` 的 skill 会挤在同一个分数上，排序等于没有。

需要自定义打分就重写 `can_handle(files) -> float`。

### ParamSpec —— 表单控件

| `type` | 界面控件 | `default` 的形状 |
|---|---|---|
| `number` | 数字输入框 | `5` |
| `text` | 文本框 | `"abc"` |
| `bool` | 开关 | `True` |
| `select` | 下拉（配 `options=[...]`） | `"a"` |
| `range` | 「起 — 止」两个框 | `[0, 100]` |
| `column` | 列名下拉，**自动填入当前文件的列** | `None` |
| `columns` | 列名多选 | `None` |

`column` / `columns` 是专门为实验数据做的：用户选中文件后，这两种控件会自动
列出该文件的所有列名，不用手打。

---

## SkillContext —— 你能拿到什么

```python
ctx.files                    # list[FileRef]，多文件 skill 用
ctx.file / ctx.path          # 单文件快捷入口
ctx.params                   # dict，已按 ParamSpec 转好类型
ctx.param("key", default)    # 取参数，None 时回落到 default

ctx.load_table(index=0, max_rows=None)   # → (DataFrame, Sniffed)
ctx.sniff(index=0)                       # 只要嗅探结果（含仪器抬头原文）
ctx.load_image(index=0)                  # → PIL.Image

ctx.logline("...")           # 写日志，会存进 analysis_run.log
ctx.tmp_dir                  # 临时目录，运行结束自动清理
```

`ctx.path` 一定是**能直接打开的绝对路径**。文件是被复制进工作区还是原地引用，
在这一层已经被吃掉了 —— 你不需要关心。

**关于嗅探**：`load_table()` 会自动处理仪器导出文件常见的问题：非 UTF-8 编码
（含 GB18030）、非逗号分隔（Tab / 分号 / 空格）、表头前面几行说明性抬头。
抬头原文在 `ctx.sniff().preamble` 里，仪器参数经常藏在那儿。

---

## SkillResult —— 你要返回什么

```python
SkillResult(
    metrics=[Metric("thickness", 812.4, unit="nm", quality="review")],
    tables={"profile": df},              # name → DataFrame，会存成 Parquet
    figures=[Figure("fit", svg_bytes)],  # 你自己画的图
    preview=ChartSpec(...),              # 交给前端画的图
    summary="一句话结论",
    warnings=["Y 列有 3 个 3σ 外的点"],
    logs="...",
)
```

### Metric

```python
Metric(field_name="thickness", value=812.4, unit="nm",
       label="膜厚",                 # 不填就用 field_name
       quality="review")            # validated | review | reject
```

数值进 `key_result.value_num`，字符串进 `value_text`。
`quality` 默认 `review`（待人复核）；算法足够确定时可以给 `validated`。

### ChartSpec

最省事的做法：

```python
ChartSpec.from_frame(df, x="Voltage(V)", ys=["Current density(mA/cm2)"])
```

超过 4000 点会自动等间隔抽稀，界面不会卡。手写也可以：

```python
ChartSpec(
    x_label="Voltage (V)", y_label="J (mA/cm²)",
    series=[Series("正扫", x_list, y_list, style="line"),
            Series("反扫", x2, y2, style="line+scatter")],
)
```

配色和标记与 Mengnan 的 matplotlib 规范同源（`#2470a0` 起的 12 色、2px 轴线、
刻度朝内），网页图和 Python 出图看起来是一家。

---

## 另一条通道：SKILL.md

如果你的「算法」本质上是一段给模型看的指令（比如从非结构化文本里抽参数），
可以直接放一份 `SKILL.md`：

```
workspace/skills/uvvis_reader/SKILL.md
```

```markdown
---
id: spectrum.uvvis-reader
name: UV-Vis 谱图读数
category: spectrum
version: 0.1.0
extensions: [".csv", ".txt"]
outputs:
  - field_name: bandgap
    label: 光学带隙
    unit: eV
  - field_name: transmittance_550
    label: 550nm 透过率
    unit: "%"
---

你是一个光谱分析组件。从给定的透过率谱中：
1. 用 Tauc plot 外推得到光学带隙
2. 读出 550 nm 处的透过率
无法确定的字段直接省略，不要估算。
```

平台会把文件解析成紧凑文本喂给模型，要求它按 `outputs` 返回 JSON，校验后入库。
这条通道产出的结果一律标 `source='ai'`、`quality='review'`，等人复核。

**没有配模型时**，这类 skill 仍然出现在列表里，但标注为「需配置模型」，
点运行会明确报错 —— 不会假装跑通。

---

## 更简的写法

模块里也可以直接定义 `Skill` 子类（会自动实例化，可以有多个）：

```python
class SkillA(Skill):
    spec = SkillSpec(...)
    def run(self, ctx): ...

class SkillB(Skill):
    spec = SkillSpec(...)
    def run(self, ctx): ...
```

或者提供 `register(registry)` 函数完全自己控制。

---

## 调试

- 加载失败的 skill 会在**设置页**列出错误和堆栈，**不会拖垮整个服务**
- 界面上的「试跑」按钮只跑不写库，改算法时用它
- `ctx.logline()` 的内容存在 `analysis_run.log`，`GET /api/runs/{id}` 能取到
- 运行失败会记录 `analysis_run.status='failed'` 和完整 traceback，不会静默吞掉

---

## 内置样例

| 文件 | 看什么 |
|---|---|
| `app/skills/builtin/table_preview/skill.py` | 一个完整可运行的 skill，嗅探 + 统计 + 出图 |
| `app/skills/builtin/thickness/skill.py` | 只留一个 `analyze()` 函数待填的模板 |
| `app/skills/builtin/spectrum/skill.py` | 同上 |

膜厚和光谱这两个模板的接法：实现 `analyze(df, params, ctx) -> dict`，
然后把文件顶部的 `ALGORITHM_READY` 改成 `True`。就这两步。
