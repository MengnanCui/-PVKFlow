# 给平台加一个功能模块

> 这份文档是写给**你和你的模型**看的。整段贴进对话框，然后说
> 「照这个写一个 XXX 模块」就行。

## 一句话

你写一个 `module.py`，放进 `workspace/modules/`，点一下「重载」。
界面自己长出来。

**你不写 HTML、不写 CSS、不写 JS，也不改平台的任何一行代码。**

这不是为了省事。这是为了让你加的功能和平台其它部分长得一模一样 ——
面板结构、控件样式、图注、下载菜单、ⓘ、左右对齐，全部由平台按你的声明渲染。
你没有画界面的机会，所以风格漂不了。

---

## 三分钟上手

```
workspace/modules/
└── my_module/
    └── module.py        ← 只要这一个文件
```

```python
from app.modules.base import Module, ModuleSpec, Control, Panel, Curve, Op


class MyModule(Module):
    spec = ModuleSpec(
        id="pl.integral",              # 小写，带点分段，全局唯一
        name="荧光强度",                 # 界面上显示的名字
        version="1.0.0",

        # ① 控件：用户能调的参数
        controls=[
            Control("band", "波段", "band", default=[700, 800], unit="nm",
                    help="对这一段积分"),
        ],

        # ② 面板：一格图。左右并排、等高对齐由平台保证
        panels=[
            Panel("main", "荧光强度 vs 时间",
                  uses=["band"],                       # 控件画在这一格上面
                  live=Op.band_integral(band="band")),  # ← 见下面「两档」
        ],

        # ③ 批处理：声明了它，你的曲线自动进长表、对比页、时刻切片、导出脚本
        batch_curves=[Curve("pl_integral", from_panel="main")],
    )


MODULE = MyModule()          # 别忘了这一行
```

放好文件 → 打开「设置 → 功能模块」→ 点「重载」。完事。

想更快就直接复制 `workspace/modules/_template/` 改。

---

## 两档面板：能拖 vs 松手才算

### A 档 —— 用平台算子拼，**拖控件时实时出结果**

面板写了 `live=Op.xxx(...)` 就是 A 档。拖滑块时算子在浏览器里跑，
实测 **2 ms**，曲线连续跟着手走。

```python
Panel("main", "荧光强度 vs 时间", uses=["band"],
      live=Op.band_integral(band="band")),
```

`Op.band_integral(band="band")` 的意思是：
「用 `band_integral` 这个算子，它的 `band` 参数从 key 为 `band` 的控件取值」。

**现有的算子**（这是全部，不能自己加）：

| 算子 | 参数 | 要什么控件 | 算什么 |
|---|---|---|---|
| `Op.band_integral(band=...)` | `band` | `band` 类型 | 波段内强度对波长积分 |
| `Op.wavelength_slope(center=..., half=...)` | `center` `half` | 都是 `number` 类型 | 窗口内最小二乘拟合的斜率 |

> **为什么不能自己加算子？** 一个算子要同时有 JS 和 Python 两份实现
> （拖动时用前者，批处理时用后者），外加一条「两份必须逐点相同」的测试。
> 少写一边的后果是：你在界面上拖出来一个数、存进库里是另一个数，
> **而且没有任何报错**。所以加算子是平台维护者的活 —— 需要新算子就去提。

### B 档 —— 写自己的 Python，**松手才算**

算子拼不出来的（峰拟合、FFT、任意 numpy），去掉 `live=`，写一个 `compute()`：

```python
import numpy as np
from app.modules.base import Module, ModuleSpec, Control, Panel, PanelData


class PeakFit(Module):
    spec = ModuleSpec(
        id="pl.peak_fit", name="荧光峰位", version="1.0.0",
        controls=[Control("band", "波段", "band", default=[700, 800])],
        panels=[Panel("peak", "峰位 vs 时间", uses=["band"],
                      y_label="峰位 (nm)")],       # 没有 live= → B 档
    )

    def compute(self, ctx):
        out = super().compute(ctx)        # ← A 档面板归基类管，先拿到手
        lo, hi = ctx.param("band")
        mask = (ctx.lam >= lo) & (ctx.lam <= hi)
        peak = ctx.lam[mask][np.argmax(ctx.M[mask], axis=0)]
        out["peak"] = PanelData(x=list(ctx.t), y=list(peak),
                                y_label="峰位 (nm)")
        return out
```

**B 档不是降级。** 平台自己的膜厚模块就是这样 —— FFT 本来就贵，
拖着算既没必要也做不到。规律是：**便宜的操作能拖，贵的松手才算。**

一个模块可以两档混用：一格 A 档一格 B 档，完全没问题。

---

## `compute()` 里你拿得到什么

```python
def compute(self, ctx):
    ctx.lam       # 波长轴，np.ndarray，形状 (n_lambda,)
    ctx.M         # 矩阵，np.ndarray，形状 (n_lambda, n_time)
    ctx.t         # 时间轴，np.ndarray，形状 (n_time,)
    ctx.param("band")           # 取控件的值
    ctx.op("band_integral", band=[700, 800])   # 也能调平台算子
```

矩阵已经**载入好、缓存好**了 —— 编码、分隔符、仪器抬头、UTF-16、
各种格式变体，平台都处理完了。你直接拿 numpy 数组算。

返回 `{面板 id: PanelData}`，`PanelData` 的 `y` 必须和 `ctx.t` **一样长**。

---

## 控件类型

| 类型 | 值长什么样 | 界面上是什么 |
|---|---|---|
| `"band"` | `[起, 止]` | 两个数字框 + 两个滑块 |
| `"number"` | 一个数 | 一个数字框 + 一个滑块 |
| `"select"` | 选项之一（要给 `options=[...]`）| 下拉 |
| `"bool"` | True / False | 勾选框 |

**上下限跟着数据走，别写死：**

```python
Control("center", "波长", "number", default=950, range_from="lambda")
```

`range_from="lambda"` 表示上下限跟着这份数据的波长轴。
写死 `max=1120` 只对你这台光谱仪成立，换一台就不对了。

---

## `Panel.info` —— 标题旁边那个 ⓘ

```python
Panel("main", "荧光强度 vs 时间", uses=["band"], info="integral")
```

`info` 填的是 `web/js/glossary.js` 里的术语 id。点开 ⓘ 会显示那条术语的
「是什么 / 为什么这么定」，下面还能接着问模型。

术语表里没有的词，验证器会拦住你 —— 要么把术语加进去，要么把 `info` 去掉。
**指向一个不存在的术语，点开是空的，那比没有 ⓘ 更糟。**

---

## 装上之前会被检查什么

验证器会跑一遍，**不通过就装不上**。它检查：

1. `id` / `name` / `version` 格式对不对，`id` 有没有和别人重名
2. 控件 key 唯一、类型认识、`band` 的默认值是两个数
3. `Panel.uses` 里的每个 key 都真的有对应控件
4. `Op` 绑定的控件存在，而且**类型对得上**
   （`band_integral` 的 `band` 参数必须绑到一个 `band` 类型的控件）
5. `Panel.info` 在术语表里存在
6. `batch_curves` 指向的面板存在
7. **拿一份数据真跑一遍**：`compute()` 不崩、每个声明的面板都返回了、
   曲线长度和时间轴对得上、不是全空
8. 没往自己的目录外面写文件

报错会**逐条**告诉你是哪个字段、错在哪、合法值是什么，比如：

```
✗ panel "integ" 的 uses 里写了 "integral"，但 controls 里没有这个 key。
  现有的 controls：slope_center, slope_half, integ
  你是不是想写 "integ"？
```

**把整段报错贴给你的模型，让它改，然后再导入一次。** 这个循环设计出来
就是给弱模型用的 —— 它不需要聪明，只需要看得懂反馈。

---

## 怎么发给别人

「设置 → 功能模块 → 导出」，得到一个 zip。企业微信、U 盘、共享盘发过去。
对方「设置 → 功能模块 → 导入」，选那个 zip。

**不需要 Git，不需要分支，不需要合并。** 一个模块就是一个文件夹。

---

## 活示范

平台自带的「特殊处理」就是照这份文档写的：
`app/modules/builtin/special_processing/module.py`。

它两个面板都是 A 档，**一行算法都没有**。要写新模块，
先把那个文件打开看一眼，多半比读这份文档还快。

---

## 边界（说清楚，不含糊）

- **模块是 Python，能跑任意代码。装谁的模块就是信任谁。**
  验证器那条「没往目录外写文件」挡的是失误，**不是恶意** ——
  它不是安全边界，我们也不假装它是。
- 平台自带的模块不能卸载。
- 算子集不能自己加（见上）。
- 一个模块崩了不会带走整个平台，也不会带走整批批处理 ——
  界面上会显示是**哪个模块**出了什么问题。
