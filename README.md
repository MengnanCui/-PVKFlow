# HTE Studio

高通量实验数据平台。本地运行，双击就开。

**数据处理 → 数据存储 → 构效关系** —— 三层在时间上有先后。
当前版本把前两层做扎实，第三层留好入口和数据形态。

---

## 在 Windows 上运行

1. 装 [Python 3.11+](https://www.python.org/downloads/)，安装时**勾选
   "Add python.exe to PATH"**
2. 双击 `run.bat`

首次运行约一分钟（建虚拟环境 + 装依赖），之后几秒就开。浏览器会自动打开。

macOS / Linux 用 `./run.sh`。

> 所有数据都在 `workspace/` 这一个目录里 —— 数据库、复制进来的原始文件、
> 派生结果、你自己的 skill、模型配置。整个拷走就能换机器。
> 想放到别的盘：设环境变量 `HTE_WORKSPACE=D:\HTE`。

---

## 吸收光谱 & 膜厚

「数据处理」页上有四个子功能，目前做通的是**吸收光谱 & 膜厚**。它吃的是一个
波长 × 时间的宽表（首列波长，其余每列一个时刻；转置的也认），点进样品后有三个模块：

| 模块 | 内容 |
|---|---|
| **光谱处理** | 波长–时间强度热力图；归一化强度 vs 波长（多时刻叠加） |
| **膜厚处理** | 波数–时间干涉条纹图（全波段 + 指定波段）；光学厚度 vs 时间 |
| **特殊处理** | 某波长处的谱斜率 vs 时间；某波段积分 vs 时间 —— **拖滑块实时更新** |

热力图在服务端渲染成 PNG（10⁶ 个 SVG 矩形会卡死浏览器），坐标轴是矢量的；
曲线全部在前端画，可悬停读数、框选放大、跟随明暗主题。特殊处理用光谱处理已经
载入的抽样谱在本地算，拖动时 0 延迟，停手后自动换成全分辨率的精确结果。
详见 [架构文档](docs/ARCHITECTURE.md#图怎么画位图归位图曲线归曲线)。

> **光学厚度曲线的算法尚未接入**，接口已就位（`GET /api/spectra/{id}/thickness`），
> 界面上如实标注为「待接入」。接上之后前端不用改。

想立刻试：`python sample_data/make_insitu.py` 生成三个样例矩阵，再导入 `sample_data/`。

## 五分钟走一遍

1. **导入** —— 「数据处理」→「浏览本机目录」，选一个实验数据目录。
   先给扫描预览：哪些会被复制、哪些原地引用、每个文件解析出的样品名。
   确认后才写入。
2. **选文件** —— 左栏点一个文件，中间出现数据预览和曲线，右栏给出识别结果与候选处理。
3. **处理** —— 选一个 skill，填参数（表单是根据 skill 声明自动生成的），
   点「试跑」先看结果，或「运行并保存」写入数据库。
4. **看存储** —— 「数据存储」页显示真实的落地情况：复制了多少、引用了多少、
   积累了哪些关键字段。

仓库里带了 `sample_data/`，可以直接拿它试。

---

## 导入时文件怎么落地

| 类型 | 怎么处理 | 为什么 |
|---|---|---|
| 文本类（csv / txt / dat / json / xlsx …） | **复制**进 `workspace/raw/`，按 sha256 内容寻址 | 小、易丢、经常被人手改，复制一份才谈得上可复现。同内容自动去重 |
| 图像类（png / jpg / tif …） | **不复制**，只登记绝对路径 + 哈希，另生成缩略图 | 动辄几百 MB，搬一遍不划算 |

代价说清楚：引用型文件被移动或改名后会断链。界面上会标成「丢失」，
点「巡检」可以主动检查。**不会静默失败。**

两个名单都能在「设置 → 导入策略」里改。

> 拖拽上传是个例外：浏览器出于安全不给真实路径，所以那条通道**一律复制**，
> 图像也不例外。想让图像走原地引用，用「浏览本机目录」导入。

---

## 加一个处理 Skill

这是这个平台的核心机制。把目录丢进 `workspace/skills/`，点「重载 Skill」即可：

```
workspace/skills/my_thickness/skill.py
```

```python
from app.skills.base import Skill, SkillSpec, ParamSpec, OutputSpec, FileMatch, SkillResult, Metric

class MyThickness(Skill):
    spec = SkillSpec(
        id="thickness.profilometer", name="台阶仪膜厚",
        category="thickness", version="1.0.0",
        accepts=FileMatch(extensions=[".csv"]),
        params=[ParamSpec("baseline", "基线区间", "range", default=[0, 10], unit="%")],
        outputs=[OutputSpec("thickness", "膜厚", unit="nm")],
    )

    def run(self, ctx):
        df, _ = ctx.load_table()          # 编码/分隔符/仪器抬头已经嗅探好
        return SkillResult(metrics=[Metric("thickness", my_algorithm(df), unit="nm")])

SKILL = MyThickness()
```

**参数表单、结果卡片、图表全部自动生成，你不用碰任何前端代码。**

完整说明：[`docs/SKILL_CONTRACT.md`](docs/SKILL_CONTRACT.md)

### 膜厚 / 光谱这两个模板

`app/skills/builtin/thickness/` 和 `spectrum/` 已经把整条链路接通了 ——
文件解析、参数表单、结果落库、出图、版本追溯都在，只差算法本身。
接法是两步：

1. 实现文件里的 `analyze(df, params, ctx) -> dict`
2. 把文件顶部的 `ALGORITHM_READY` 改成 `True`

在此之前它们在界面上标为「待接入」，点运行会明确报错。**不会假装跑通。**

---

## 模型（可选）

平台的「智能」部分默认由**确定性规则引擎**承担：文件类型识别、skill 推荐、
数据质量检查。这部分不需要联网、不需要密钥，现在就是可用的。

配了模型之后额外获得：结果解释、问答、以及 `SKILL.md` 那条通道。

「设置 → 模型」粘贴 OpenAI 兼容格式的配置：

```json
{
  "providers": {
    "vllm-local": {
      "baseUrl": "http://your-host/v1",
      "api": "openai-completions",
      "apiKey": "sk-...",
      "compat": { "supportsDeveloperRole": false },
      "models": [{ "id": "Qwen3.6-27B", "name": "Qwen 3.6 27B", "input": ["text","image"] }]
    }
  }
}
```

密钥写进 `workspace/config/providers.json`。**这个目录在 `.gitignore` 里，
密钥不会被提交进仓库**，接口返回时也一律打码。

界面上每条结论都标注来源是「规则」还是具体模型名。没配模型时，AI 面板会
如实说「还没有配置模型」，不编造回答。

---

## 目录

```
run.bat / run.sh     一键启动
app/                 后端（FastAPI + SQLite + Parquet）
web/                 前端（原生 ES Module，零构建）
docs/                架构、数据模型、Skill 契约
sample_data/         可以直接拿来试的样例数据
workspace/           运行期数据（gitignore）
```

## 开发

```bash
./run.sh                                   # 起服务
.venv/bin/python -m pytest tests/ -q       # 跑测试
```

前端改完刷新页面即可，没有构建步骤。

## 文档

- [架构](docs/ARCHITECTURE.md) —— 模块划分与三条设计原则
- [数据模型](docs/DATA_MODEL.md) —— 为什么 `key_result` 是长表
- [Skill 契约](docs/SKILL_CONTRACT.md) —— 怎么接入你的算法

## 当前不做什么

- 构效关系的实际分析功能（X/Y 映射、相关性、分组比较）—— 只留入口与数据形态
- 静默监听目录、自动跑 skill
- 打包成单个 exe
