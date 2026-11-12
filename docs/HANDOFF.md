# 交接文档 —— 接手这个项目的人（或模型）从这里开始读

> 写给**任何**接着做这个项目的人或模型（不论是哪家的模型）。读完这一份，你应该能回答：
> 这个平台是做什么的、为什么这么设计、每个菜单里在发生什么、代码在哪、怎么加东西而不破坏别的东西、
> 改完怎么验证、怎么记录。
>
> 文档分工：
> - **本文（HANDOFF.md）**：思路、约定、每个菜单的逻辑、扩展方法、踩过的坑 —— 人写的「为什么」
> - **`docs/REFERENCE.md`**：全部接口、页面、后台任务、智能体工具、数据表、环境变量、QFLS 阈值 —— **从代码生成**的「是什么」，
>   不许手改，`tests/test_reference_doc.py` 守着它和代码一致
> - 深入某一块：`docs/ARCHITECTURE.md`（架构、图怎么画、缓存、安全）、`docs/DATA_MODEL.md`（数据模型）、
>   `docs/MODULE_AUTHORING.md`（写功能模块）、`docs/plan/index.html`（项目计划、用户要求与我的决定的登记表）
> - **`CLAUDE.md` / `AGENTS.md`**：在这个仓库里工作**必须遵守**的规则（每次提交写报告等）

---

## 0. 先读这一节：硬规则

这些不是建议。违反任何一条，用户都会发现，而且会很在意。

1. **每一次提交都要有一份修改报告** `docs/plan/changes/C###_日期_提交.md`，和代码在同一个提交里。
   报告里**分清「用户明确指定的」(P-##) 和「我自行决定的」(D-##)**；新登记项同步进 `docs/plan/index.html` §3 / §4，
   附录加一行。编号只增不改。模板见 `CLAUDE.md`。
2. **凡是不是用户给的数值、默认值、阈值、算法口径，都是「自行决定」**，要登记；影响数值或科学结论的标「建议你确认」，
   并在回复里点名告诉用户。拿不准用户是不是这个意思，标「推断」。**不要把用户没说过的写成「你要求的」。**
3. **`app/analysis/fringe_ot.py` 的 `DEFAULTS` 是冻结的规范原文，不许改。** 平台想用别的值，走 override。
4. **原始数据永远只读。** 导入时复制的是只读副本，引用的是只读引用；QFLS 处理只用 `"rb"` 打开原始文件，输出写独立目录。
5. **跑测试**：`.venv/bin/python -m pytest -q`（Windows：`.venv\Scripts\python -m pytest -q`）。**改了前端，至少用 Playwright
   把受影响的页面打开一遍看 console**（`tools/qa/` 里有现成的验收脚本）。
6. **提交信息、报告里不写模型名称。** 提交信息末尾带用户要求的署名行（见 `CLAUDE.md`）。
7. 改了接口 / 页面 / 智能体工具 / 表 / 环境变量 / QFLS 阈值 → 跑 `.venv/bin/python tools/gen_reference.py` 重新生成参考手册。
8. 每个阶段（用户一轮需求做完）写阶段报告 `docs/plan/phases/P##_…md`，更新 `index.html` 的版本号、§2 功能现状、§5 时间线、
   「等你定」。

---

## 1. 这个平台是什么

**HTE Studio（仓库名 PVKFlow）**：钙钛矿（PVK）高通量实验（HTE）的本地数据平台。**本地运行、双击就开、不联网也能用、数据全在自己机器上。**

- **用户**：项目负责人（材料 / 器件方向的研究者）和课题组同事。同事用 **Windows**，双击 `run.bat` 启动；
  会写 Python 的同事可以写「功能模块」给平台加算法。
- **数据**：
  - **原位吸收光谱矩阵**（波长 × 时间，仪器导出的 `Data.csv`）→ 热力图、干涉条纹、光学膜厚、谱斜率、波段积分 → 批处理对比
  - **QFLS 成像**（准费米能级分裂，PL 成像得到的 eV 矩阵，按 实验 / 批次 / slot / point 组织）→ 逐点指标、评分、排名、对比
- **主线**：**数据处理 → 数据存储 → 构效关系**（三层在时间上有先后：先把处理和存储做扎实，再谈构效关系）。
  旁边两条：**AI**（抽屉问答 + 智能体）、**QFLS 成像**（一条独立的、基于文件的流水线）。
- **明确不做**（截至 P17）：不静默监听目录、不自动跑处理（所有处理都要人点一下）；不打包 exe（要先装 Python 3.11 – 3.15）；
  没有登录和权限体系（所以只监听 127.0.0.1）；AI 抽屉里模型的操作只渲染成卡片、人点了才生效。

技术栈：**FastAPI + SQLite + 原生 ES Module 前端（零构建）+ numpy / pandas / matplotlib / Pillow**。
没有 Node 构建链、没有前端框架、没有 scipy（统计和连通域都是自己写的）。

---

## 2. 设计思想（所有子系统共用的十条）

接手的人最容易在这里走偏 —— 每一条后面都有一次真实的教训。

1. **原始数据永不被修改；派生物都能重算。** 工作区（`workspace/`）是全部持久状态，整个目录可以打包带走。
   缓存（npz、PNG）随时可删，代价只是慢一次。
2. **没有模型，平台也完整可用。** 导入、处理、批处理、对比、导出、QFLS 全不依赖模型。模型只做解释、总结、提议；
   **会改东西的操作一律先给人看、人点了才做**（AI 抽屉的动作卡片、智能体的审批）。
3. **一次选择是一个「筛选式」，不是一串 ID。** 上千个样品的量级上 ID 列表放不进 URL、一周后看不出选了什么、新导入的进不来。
   数据处理页、构效关系页、智能体、批处理共用同一种筛选式（`app/storage/selection.py`）。
4. **结果是长表（EAV）+「当前值」。** `key_result` 只追加；`key_result_current` 视图给出每个 (样品, 字段) 的当前值
   （最后写入且没被作废的）。新增一个测量字段**不改 schema**。作废最新的就退回上一次。
5. **每个结果都能追溯**：哪次运行（`analysis_run`）、哪个模块哪个版本、什么参数、从哪个文件算出来。
6. **加一个算法，界面免费出现。** 功能模块只交算法和声明（控件、面板、批处理曲线），平台按声明渲染界面、进批处理、进对比页、
   进导出脚本。同事**不碰前端代码**，所以风格漂不了。
7. **位图归位图，曲线归曲线。** 10⁶ 像素的热力图服务端出 PNG；几百个点的曲线前端 SVG（能悬停、框选、导出、跟主题）。
   前端只做能和后端逐点对照的简单算术（`ops.js` ↔ `ops.py`，有测试比对）。
8. **只说做得到的事，报错说人话。** 错误结构统一 `{error:{message,kind,detail}}`，`message` 是给人看的中文；
   界面上的说明文字只写真实行为（「重新提交会跳过」这种做不到的话被删过）。
9. **页面有生命周期。** 页面常驻、切走只隐藏；所有异步回来都要先确认「我还在这一页、这还是最新的一次」
   （`ctx.alive()`、`latest()`）。见 §6.3。
10. **单机单用户，够用就好。** 后台任务一个线程池 + 一张表（不上 Celery）；数据库 SQLite + WAL；没有 ORM；
    能用 numpy 向量化写的不引新依赖。**但要把边界写清楚**（文档「边界」一节、界面上的提示）。

---

## 3. 术语（代码里的名字 ↔ 界面上的中文）

同一个概念全平台只用一个中文名。新写界面文字时照这张表；ⓘ 浮层的解释在 `web/js/glossary.js`（27 条，用户还没审）。

| 代码 / 表 | 界面上叫 | 是什么 |
|---|---|---|
| `sample` | 样品 | 实验对象。身份 = (名字, 批次)；同名不同批次是不同样品 |
| `batch`（样品的） | 批次 / 样品号 | 命名规则从文件夹 / 文件名拆出来的那一段，如 `ZG0013`。**不要和「批处理」混** |
| `measurement` | 测量 | 一个样品的一次测量（带测量时间）；一个子文件夹 = 一次测量 |
| `artifact` | 原始文件 | 文件的身份（sha256）+ 落地方式：`copy`（复制进工作区）/ `reference`（原地引用）。找不到 = `missing` |
| `analysis_run` | 一次处理 / 运行 | 用了哪个模块、什么版本、什么参数；批处理 = 一个父运行 + 每个样品一个子运行 |
| `key_result` | 关键结果 | 长表：(样品, 字段名, 数值, 单位, 来源运行, 审核状态) |
| `key_result_current` | 当前值 | 每个 (样品, 字段) 最后写入且没被作废的那条 |
| `quality` | 审核：可信 / 待查 / 作废 | `validated` / `review` / `reject` |
| 筛选式（filter） | 筛选 | `{time, sample, folder, fields…}` 的 dict，不是 ID 列表（`app/storage/selection.py`） |
| `sample_set` | 样品集 | 动态（存筛选式）/ 钉死（存 ID）。可改名、钉死、删除 |
| 功能模块（module） | 功能 | `ModuleSpec`：控件 + 面板 + 批处理曲线。A 档（平台算子，前端实时）/ B 档（自己的 compute，后端算） |
| 配方（recipe） | 配方 | 批处理时每个模块用什么参数 |
| 批处理（batch run） | 批处理 / 对比 | 对一批样品跑一遍配方；结果在「批处理结果」页（对比页）看 |
| `task` | 后台任务 | 批处理、QFLS 处理；一次一个，可停 |
| 光学厚度（OT） | 膜厚 | 干涉条纹 FFT 提取（`fringe_ot.py`，规范冻结） |
| AI 抽屉 | AI 助手 | 右侧抽屉：带着当前页面的数据问答 |
| ⓘ | ⓘ | 术语浮层，能带着读数问模型 |
| 智能体（agent） | 智能体 | 模型自己调工具做事；询问 / 自动；直接执行 / 先出计划 |
| 专用目录 | 专用目录 `agent/` | 智能体唯一能写的地方 `workspace/agent/` |
| 平台核心 | 平台核心 | 安装目录里、工作区以外的一切；智能体永远不能写 |
| QFLS | QFLS（准费米能级分裂） | PL 成像得到的 eV 矩阵 |
| 实验 / 批次哈希 / slot / point | 同左 | QFLS 数据的四层目录。**这里的「批次」是目录名，和样品的「批次」不是一回事** |
| 帧（frame） | 帧 / 样点 | 一个 `0-…txt` = 一张 QFLS 图 = 一个样点。qvdf 里的「60 帧」是采集时合成用的帧数（D-162 推断） |
| 物镜 | 物镜 | 20X / 100X；**不同物镜的绝对值不能直接比** |
| 组内 z | 组内 z | 同实验、同物镜里的 (x − 组均值) / 组标准差 |
| 处理编号 | 处理结果 | QFLS 一次处理的 `qr_…`，对应一个输出目录 |

---

## 4. 每个菜单：做什么、怎么进出、页面里的逻辑

侧栏三组：**平台**（数据的正向流程：总览 → 数据处理 → 数据存储 → 构效关系）、**分析**（回头看、或自成一套的：对比历史、
QFLS 成像、智能体）、**系统**（设置）。另有三个**下钻页**不在侧栏（样品、批处理结果、QFLS 样点）：页头右上角第一个按钮是「← 你来的那一页」，
侧栏高亮跟着来处走（从构效关系点进样品，亮的是构效关系）；直接打开链接 / 刷新时回页面声明的父页（`meta.parent`、`meta.parentArg`，`app.js` 的 `backOf`，C077）。
切页有方向：点进下钻页从右推进来、返回从左回来、侧栏换页从下面浮上来（C078，§6.3）。
地址栏 `#页面id/参数`，能刷新、能后退、能分享。完整清单见 `docs/REFERENCE.md` §1。

右上角的 **AI 助手**（抽屉）在每个页面都在：它看到的是「当前页面的数据范围」（数据处理页的筛选 / 勾选、对比页的那次批处理……）。

### 4.1 总览 `#overview`（`pages/overview.js`）
- **做什么**：只显示库里真实存在的数（样品、原始文件、关键结果、待复核）；有找不到的文件 / 失败的处理时给提示和入口；
  三层结构的入口；最近的处理；已有的关键字段。
- **进出**：首页。各卡片 / 行点进对应页面；「最近的处理」一行点进那次对比（单独跑的点进样品，C077）。
- **逻辑缺口**（已知）：没提 QFLS 和智能体；「构效关系」描述还写着「后续阶段」。

### 4.2 数据处理 `#process`（`pages/process*.js`）
- **做什么**：选一个功能（目前只有「吸收光谱 & 膜厚」可用；QFLS 那格跳到 QFLS 成像页；荧光两格「规划中」）→
  **导入**（浏览本机目录 → 扫描预览 → 确认写入；或拖拽上传 = 一律复制）→ **筛选**（时间 / 样品号 / 文件夹三维 + 字段）→
  **勾选**（点选、拖刷、Shift 范围、键盘）→ **批处理**（选配方 → 后台任务 → 跳到批处理结果页）。
- **状态**：筛选式 `S.filter`（模块级，离开再回来还在）、勾选 `S.checked`（改筛选或切功能时清空）、样品集（存 / 点回 / 改名 / 钉死 / 删除）。
- **进出**：点样品行 → 样品页；批处理 → 批处理结果页；当前筛选会被构效关系页、AI 抽屉读到（`web/js/scope.js`）。
- **文件**：`process.js`（入口、功能页签、列表）、`process-filter.js`（筛选面板、样品集）、`process-select.js`（勾选手势）、
  `process-run.js`（批处理对话框、AI 卡片发起的批处理）、`process-state.js`（共享状态与 hooks）。
- 手点「直接对比」和 AI 卡片发起的批处理是同一条路：提交后落到批处理结果页、在那里看进度（C077 合并）。

### 4.3 样品 `#sample/<artifact_id>`（下钻，`pages/sample.js`）
- **做什么**：一个样品的全部处理，从上到下：**光谱处理**（手写：热力图 + 多时刻叠加谱）→ 每个**功能模块**一节（膜厚、特殊处理、
  同事装的，界面按模块声明渲染：`components/module-panel.js`）→ **关键结果**（当前值 + 历史，在这里审核：可信 / 待查 / 作废）。
- **逻辑**：A 档面板拖控件时浏览器里实时算（`ops.js`），B 档松手后打后端（`/api/modules/{id}/compute`）；
  热力图是服务端 PNG（`/api/spectra/{id}/heatmap.png`），曲线是前端 SVG。
- **进出**：从数据处理、存储页、构效关系的点 / 离群点、批处理结果页的样品行、总览进来；「返回」回你来的那一页（C077）。
- **速度**：模块的全分辨率计算按（模块、版本、文件、内容 sha、参数）缓存在内存里，回到同一个样品不再重算（`app/modules/results_cache.py`，C078）。

### 4.4 数据存储 `#storage`（`pages/storage.js`）
- **做什么**：全库的文件清单与搜索、占用、复制 / 引用两种落地方式的统计；**断链巡检**（引用的原文件被挪走了的列出来）。
- **进出**：点文件 → 样品页。

### 4.5 构效关系 `#relation`（`pages/relation.js`）
- **做什么**：任选两个字段 X / Y：散点（按批次着色）+ 皮尔逊 / 斯皮尔曼（带 p 值、95% 区间，`app/analysis/stats.py`，对着 scipy 验过）
  + 拟合线；按批次的箱线图（离群点点得进样品）；全部可用字段的清单；就地审核结果。
- **数据**：读 `key_result_current`（当前值），后端透视成「每样品一行」（`app/storage/relation.py`）；
  样品范围可选：全部 / 数据处理页筛出的 / 数据处理页勾选的。
- **还没做**（§7 E2）：异常识别、让模型写总结。

### 4.6 批处理结果（对比页）`#batch/<run_id 或 task_id>`（下钻，`pages/batch*.js`）
- **做什么**：等任务跑完 → 叠图 / 分位数带（每个模块声明的批处理曲线各一格）→ 两个时刻窗的切片对比（柱状图 + 表）→
  改参数重跑 → 导出 `plot.py + data.csv`（可先预览）→ 钉住的 AI 分析。
- **叫法**：侧栏和文档里叫「对比」，页面标题叫「批处理结果」—— 同一个东西。
- 跑完后地址栏从 `#batch/task_…` 换成 `#batch/run_…`；运行状态表里一行点进那个样品（C077）。
- **文件**：`batch.js`（入口、等任务、样品表、钉住）、`batch-curves.js`（叠图）、`batch-slices.js`（时刻切片）、
  `batch-recipe.js`（参数与重跑）、`batch-state.js`。

### 4.7 对比历史 `#history`（`pages/history.js`）
- **做什么**：每一次批处理都在这里，标出没跑完的（已停止只有一部分 / 失败 / 进行中），点进批处理结果页。
- **改名 / 删除**（C077，`app/batch_history.py`）：删之前确认框列出会带走什么（样品数、关键结果 —— 其中几条是当前值、几条审核过、
  曲线表、钉住的 AI 分析）。删的是这次写的关键结果，样品的当前值退回上一次对比的；原始数据、样品、别的对比、AI 对话不动；
  还在跑的不能删（D-199，**待你确认**）。

### 4.8 QFLS 成像 `#qfls/<标签>`（`pages/qfls*.js`）+ QFLS 样点 `#qfls-point/<处理编号>/<样点key>`
- **做什么**：独立流水线（不进样品库）。顶栏：选看哪次处理 · 处理新数据…（浏览目录 → 扫描预览：认出什么、跳过什么和原因 → 开始）·
  生成演示数据 · 下载全部样点 CSV / README · 删除（只删输出目录）。
- **四个标签**：**总览**（四个数、等级分布表、按物镜分开的箱线图、扫描报告、全局素材图）/ **排名与筛选**（任一指标排序、
  原值或组内 z、异常整行标色、勾选加入对比）/ **多点对比**（2 – 6 个：同物镜同色标热图、直方图 / 剖面 / 变异函数叠加、指标对照）/
  **方法说明**（读这次处理用的参数，逐条写算法和阈值）。
- **筛选**（实验、物镜、等级、异常类型、只看异常、搜编号）在四个标签和样点页之间共用；「导出当前筛选」导出的就是筛出来的。
- **样点页**：身份、异常、5 张图 + 汇总图、综合分逐项扣分、全部指标（值、组均值、组内 z、能否跨物镜比）、qvdf 元数据；
  上一个 / 下一个走当前筛选的顺序。
- **限制常驻**：每个标签顶上都写着「不同物镜不可比、单光强不做 JV」。
- **状态**：`qfls-state.js` 的 `S`（这次处理的 run / summary / scan / points、筛选、对比篮子；对比篮子按处理编号记在浏览器）。

### 4.9 智能体 `#agent`（`pages/agent.js`）
- **做什么**：一栏到底的轻量对话窗口。顶栏：会话下拉、＋（新会话）、**询问 / 自动**、**直接执行 / 先出计划**、模型、⚙（权限一览、
  文件夹、跑命令开关、工具清单）。每次工具调用一行（工具 · 关键参数 · ✓ 用时，点开看参数和原文）；会改东西的停下来出审批卡；
  计划模式交计划卡（按计划执行 / 改一改）；斜杠命令 `/plan /exec /ask /auto /new /undo /stop /delete /help`。
- **后端**：`app/agent/`。工具 25 个（`docs/REFERENCE.md` §4）；只写专用目录 `workspace/agent/`；平台核心永远不能写；
  跑命令在专用目录里、危险命令每次问、跑完检查核心有没有被改；会话（含停在审批上的那一轮）存 `agent_session`，刷新后能接着批。
- **有意的例外**：离开页面不停正在跑的那一轮（让它在后台干完）。

### 4.10 设置 `#settings`（`pages/settings.js`）
- **做什么**：导入策略（哪些扩展名复制 / 引用）、命名规则（文件夹名 → 样品 / 批次，带预览）、**功能模块**（已装的、导入 zip（先确认）、
  重载、卸载、导出）、**模型**（协议、地址、密钥、思考、拉取并测速、连接测试、候选地址、高级 JSON）、工作区占用与缓存清理、
  旧 Skill 残留提醒。

### 4.11 AI 助手（抽屉，`components/ai-drawer.js`）和 ⓘ（`components/info.js`）
- 抽屉：带着当前页面的数据范围问答（流式）；模型可以提议「收窄到某个筛选式」「跑一次批处理」—— 都是卡片，点了才生效；
  回答可以钉到某次对比上；对话有历史（`conversation` / `message`）。
- ⓘ：术语解释（`glossary.js`），能带着眼前的读数问模型；一条术语一条对话线。

---

## 5. 数据怎么流

```
                ┌──────────────── 主线（进数据库）────────────────┐
本机文件夹 ──导入──▶ artifact（文件身份 sha256）──▶ measurement ──▶ sample（名字 + 批次）
   │  （复制文本类 / 原地引用图像；子文件夹 = 一次测量；去重）
   │
   ├─ 样品页：一个样品、一个功能模块 ──▶ 控件 → 面板（A 档前端实时 / B 档后端算）
   │
   └─ 数据处理页：筛选式选一批 ──▶ 批处理（后台任务）──▶ analysis_run（父 + 每样品子运行）
                                                    ├─▶ key_result（长表，只追加）──▶ key_result_current（当前值）
                                                    └─▶ data_table（每样品曲线的长表 CSV / Parquet）
        对比页（batch）读 data_table 画叠图 / 分位数带 / 时刻切片；对比历史列出所有批处理
        构效关系读 key_result_current 透视成「每样品一行」做散点、相关、箱线图
        审核（可信 / 待查 / 作废）改的是 key_result.quality；作废最新的 → 当前值退回上一次

                ┌──────────── QFLS（不进数据库，只有文件）────────────┐
数据根目录 ──扫描（只 stat）──▶ 帧清单 ──逐帧：校验 → 指标 → 分数 / 等级 / 异常 → 图──▶
   workspace/qfls/<run_id>/（run.json、scan.json、points.json/.csv、summary.json、README.md、global/、points/<key>/）
   ──▶ QFLS 页面只读这个目录

                ┌──────────────── AI（可选，没配模型也不影响上面）────────────────┐
AI 抽屉：当前页面的「已知事实」（汇总 + 逐样品标量 + 曲线摘要，app/ai/context.py）+ 你的问题 → 流式回答；
         模型提议的筛选式 / 动作 → 卡片，你点了才生效；回答可钉到某次对比上（ai_pin）
ⓘ 浮层：术语解释（web/js/glossary.js）+ 带上眼前的读数问模型
智能体：模型一轮轮调工具（app/agent/tools.py）→ 只读的直接跑，会改东西的按「询问 / 自动」先问 → 会话存 agent_session
```

---

## 6. 代码地图

完整的文件树和每个文件管什么见 `docs/ARCHITECTURE.md`「后端模块」「前端」两节；全部接口见 `docs/REFERENCE.md`。这里只讲**分层规则**。

### 6.1 后端分层

```
app/main.py         装配：中间件（security）、路由、静态文件、异常 → 统一错误结构；启动时建目录 / 迁移 / 加载模块 / 收尾中断的任务
app/api/*.py        路由：收参数（请求体模型在 api/bodies.py）、调领域函数、guard() 把领域异常翻成 HTTP。普通 def。
                    流式响应共用 api/streaming.py；路由之间不互相 import
app/errors.py       领域层只抛这两种：NotFound（→404）、Invalid（→400），都带 kind；普通 KeyError / TypeError 是 bug，报 500
app/util/*          共用小工具：原子写（files）、字节 → 文本（text）、CSV 防公式（csvsafe）、时间与 id（ids：一律 UTC）
app/storage/*.py    数据库与文件落地（SQL 写在这里；schema.sql + migrations.py；catalog.py 是各页的列表 / 统计查询）
app/batch.py        批处理（后台任务）  batch_views.py 对比页的只读查询  batch_history.py 改名 / 删除
app/tasks.py        任务队列（一次一个、可取消）；重启时每种任务的收尾由它注册的 on_interrupted 做
app/modules/*       功能模块的契约、加载、验证、算子          app/analysis/*  纯算法（fringe_ot 冻结）
app/parsers/*       文件解析与位图渲染                        app/plotting/*  导出可自己跑的绘图脚本
app/ai/*            模型网关、provider 配置、给模型的上下文
app/agent/*         智能体：工具注册表、控制回路、文件权限、跑命令
app/qfls/*          QFLS 流水线（不碰数据库）
```

依赖方向：`api → (storage | batch | modules | ai | agent | qfls) → (analysis | parsers)`。领域层不 import `app.api`
（体检时核过：没有一处反向依赖）。

### 6.2 前端结构

```
web/index.html → web/js/app.js（导航、主题、健康检查、AI 抽屉）
  pages/<id>.js       一个菜单一个文件（大的拆成 <id>-*.js，共享状态放 <id>-state.js，依赖无环）
  components/*.js     跨页面复用：figure（一格图）、heatmap、facets（筛选面板）、filepicker、module-panel、ai-drawer、info（ⓘ）…
  ui.js               h() 建 DOM、render() 三态、latest()、modal / confirmDialog / toast、busy()，
                      共用小组件：metric / metricRow（指标卡）、segmented（分段控件，认方向键）、rowLink（能点进去的表格行）
  components/figure.js  一格图、selectControl（下拉）；components/field-cells.js 的 fieldTable（字段表）
  api.js              所有后端调用的包装（错误统一成 ApiError）；api.with(signal) 让这一页的请求离开时真的中止
  chart.js            自写 SVG 图：xyChart / barChart / 坐标轴 / 宽度追踪；relation-charts.js 有散点和箱线图
  css/tokens.css      **唯一定义颜色、字号、间距的地方**（明暗两套）
```

### 6.3 页面生命周期（写前端必须遵守）

页面常驻、切走只隐藏，状态在模块级变量里。所以：
- `view(host, ctx)` 的 `ctx` 有 `signal`（离开即 abort）和 `alive()`。**每个 `await` 之后、改状态或写页面之前先 `if (!alive()) return;`**
- 轮询 / 定时器 / 页面外的监听，离开时要停（`ctx.signal.addEventListener('abort', …)`）
- 会被连续触发的请求用 `latest()`：发之前取号，回来 `if (!fresh()) return;`
- 按钮请求期间 `busy(btn, true)`；会删东西的先 `confirmDialog(…, {danger: true})`
- 可点的非按钮元素给 `tabindex` + `onkeydown`（表格行直接用 `rowLink`）；颜色只用 `tokens.css` 的变量
- 加载重的页面用 `api.with(ctx.signal)` 发请求：离开时请求真的中止，抛 AbortError，`render()` 和 `alive()` 都当「过期」处理
- `render()` 第二次进同一页时旧内容先留着（顶上一条细线），新数据到了原地换；不要在 `render` 外面自己先 mount 骨架屏
- 下钻页在 `meta` 里写 `parent`（和可选的 `parentArg`），**不要自己画返回按钮** —— `app.js` 统一放在页头
- 动效只动 `transform` / `opacity`；不要用 View Transitions、背景模糊（`backdrop-filter`）、动宽高 —— 实测都掉帧（§10）
- 前端一行不超过 140 个字符；指标卡、分段控件、下拉、字段表用共用的，不要再抄一份（`tests/test_frontend_style.py` 守着）

---

## 7. 二次开发：九种最常见的改动，各改哪里

每一种都写了「改哪些文件」「哪些东西要一起改（不然就漂了）」「怎么验证」。
**通用收尾**：跑 `pytest`；动了前端跑相关的 `tools/qa/*_check.js`；动了接口 / 页面 / 工具 / 表 / 环境变量 / QFLS 阈值就
`tools/gen_reference.py`；写修改报告、登记 P / D。

### 7.1 加一个数据处理算法（功能模块）—— **首选这条路**
- **不改平台代码**：在 `workspace/modules/<目录>/module.py` 里写一个 `Module` 子类 + `MODULE = ...`，设置页点「重载」。
  完整写法见 `docs/MODULE_AUTHORING.md`（可以整段贴给模型让它照着写）；模板 `workspace/modules/_template/`；
  平台自带的两个活示范在 `app/modules/builtin/`（膜厚 = B 档、特殊处理 = A 档）。
- 契约在 `app/modules/base.py`（`ModuleSpec / Control / Panel / Curve / Op`）；装之前的检查在 `app/modules/validate.py`；
  命令行自检 `python -m app.modules.check <目录>`。
- **A 档**（声明成平台算子 `Op.*`）拖控件时浏览器里实时算；新增算子要**同时**改 `app/modules/ops.py` 和 `web/js/ops.js`，
  `tests/test_ops.py` 在 node 里逐点比对两边。**B 档**（自己写 `compute()`）松手后后端算。
- 声明 `batch_curves` 就自动进批处理长表、对比页、时刻切片、导出脚本。
- 要平台内置（随仓库发）：放进 `app/modules/builtin/<目录>/`，加测试（参照 `tests/test_modules.py`、`tests/test_module_contract_v2.py`）。

### 7.2 加一个智能体工具
- 在 `app/agent/tools.py` 写 `t_xxx(args) -> dict` 处理函数，并在 `TOOLS` 列表里加一条
  `Tool(name, 中文名, 描述, JSON schema, 风险, 处理函数, preview=…, risk_fn=…)`。
- **风险三档**：`READ` 直接跑；`WRITE` 询问模式下先问（可勾「本次不再问」）；`IRREVERSIBLE` 每次都问、记不住。
  会改东西的必须给 `preview`（审批卡上那句话，只读）。按参数升风险用 `risk_fn`（跑命令就是这样把 `rm -rf` 升成每次问）。
- 描述要写清**什么时候该用它**（模型靠描述挑工具）；结果超过 `TOOL_MAX_CHARS` 会被截断并注明。
- 要碰文件，一律经 `app/agent/files.py`（权限、真实路径核对、打码、写前备份）；不要自己 `open()`。
- 前端：工具卡上显示的「关键参数」在 `web/js/pages/agent.js` 的 `KEY_ARG`，加一项即可。
- 测试：`tests/test_agent_tools.py`（工具本身）、`tests/test_agent.py`（回路、审批）。⚙ 抽屉里的工具清单自动出现。

### 7.3 接一家新的模型服务 / 新协议
- 只要是 OpenAI 兼容或 Anthropic 协议：**不用改代码**，设置页填地址、密钥，选协议。想放进预设：
  `app/ai/openai_compat.py` 的 `PRESETS`（**只放地址，不放密钥**；`tests/test_ai.py` 守着预设里没有密钥样式的字）。
- 新协议：`app/ai/gateway.py` —— `PROTOCOLS`、`headers / chat_url / models_url`、`build_body / finish_body`（请求体）、
  `parse_whole / read_stream / _iter_text`（响应），智能体的工具调用在 `app/agent/driver.py`（`wire / to_anthropic / step`）。
  所有请求都经 `gateway.request()`（退避重试、思考参数、拒收降级都在这一层）。测试 `tests/test_gateway.py` 用 `httpx.MockTransport`。

### 7.4 改 QFLS 的阈值 / 加一个 QFLS 指标
- **阈值**：`app/qfls/metrics.py` 的 `PARAMS`（每个都是登记过的 D 项，改了要登记、要告诉用户）。处理时也可以用
  `POST /api/qfls/runs {"params": {...}}` 只覆盖这一次，用的那份会存进输出目录的 `run.json`。
- **加指标**：`metrics.compute()` 里算出来放进返回的 dict → 要进全局比较 / 组内 z 就加进 `METRICS`
  （键、中文名、单位、能不能跨物镜比）→ 要进分数就在 `score()` 和 `PARAMS["score"]` 里加一项 → 要报异常在 `metric_flags()` 里加。
  然后：`pipeline.CSV_COLS`（CSV 列）、`web/js/pages/qfls-state.js`（`fmtMetric` 的格式、`BETTER` 好坏方向、`FLAG_TEXT`）、
  `qfls-views.js` 的 `RANK_COLS`（排名表列）、`qfls-method.js`（方法说明的算法描述）、`app/qfls/pipeline.readme()`（输出目录的 README）。
- 测试：`tests/test_qfls.py`；`test_demo_known_answers_are_all_found` 守着演示数据里造进去的问题都能被标出来 ——
  改阈值时它红了，说明你改出了漏报 / 误报。

### 7.5 加一种后台任务
- 写 `fn(ctx: tasks.TaskContext) -> dict`，用 `@tasks.register("kind")` 注册（参照 `app/batch.py` 的 `run_batch`、`app/qfls/task.py` 的 `run_qfls`），
  执行体放在领域层，模块要在启动时被 import（对应的路由文件 import 了它就行）。
- 循环里调 `ctx.progress(done, total, message)` —— 它同时检查取消（协作式），被取消时抛 `tasks.Cancelled`。
- 提交：`tasks.submit(kind, params, title)` 立刻返回 task；前端轮询 `GET /api/tasks/{id}`、停止 `POST /api/tasks/{id}/cancel`。
  **一次只跑一个任务**（线程池 1 个 worker），服务重启时没跑完的会被标成失败（`reap_interrupted`）。

### 7.6 加一张表 / 改表结构
- `app/storage/schema.sql` 里加 `CREATE TABLE IF NOT EXISTS`（全新工作区直接建到最新）；**已有工作区**靠
  `app/storage/migrations.py` 的 `MIGRATIONS` 末尾追加一步（已发出的迁移不改、每一步都能重跑；升级前自动备份 `hte.db`）。
- 一起改：`docs/DATA_MODEL.md`、`tests/test_migrations.py`（如果有新迁移）、`tools/gen_reference.py` 重新生成。
- 不用 ORM：SQL 写在 `app/storage/*.py`；写操作走 `db.tx()`。

### 7.7 加一个接口
- 在 `app/api/<领域>.py` 的 `router` 上加 —— **一律写普通 `def`，不要 `async def`**（阻塞活放线程池；`tests/test_api.py` 拦 async）。
- 错误用 `ApiError(message, status, kind)` 或 `guard(fn, ...)`（把领域层的 `KeyError / ValueError / FileNotFoundError` 翻成 404 / 400）。
  **业务逻辑放领域模块**（`app/storage`、`app/batch.py`、`app/qfls`…），路由只做参数和翻译。
- 新文件的话在 `app/main.py` 的 `include_router` 列表里加上。前端在 `web/js/api.js` 加一个包装函数。

### 7.8 加一个菜单 / 页面
- 新建 `web/js/pages/<id>.js`，导出 `meta = {id, title, desc, parent?}` 和 `view(host, ctx)`（可选 `actions(nav, arg)` 放顶栏按钮）。
- 在 `web/js/app.js` 里 import、放进 `PAGES`，放进 `MAIN_NAV / ANALYSIS_NAV / SYSTEM_NAV` 之一（下钻页不放，写 `meta.parent`）。
- **遵守页面生命周期**（§6.3）；样式用 `web/css/tokens.css` 里的变量，不新造颜色；可点的非按钮元素要 `tabindex` + `onkeydown`
  （`tests/test_frontend_a11y.py` 会拦）。写一个 `tools/qa/<页>_check.js`。

### 7.9 加一种新数据类型（荧光、散射……）
两条路，按数据的样子选：
- **能变成「一个样品一份矩阵 / 曲线」的**：写解析器（`app/parsers/`，参照 `insitu_csv.py`、`matrix.py`）+ 功能模块，走主线
  （导入 → 样品 → 批处理 → 关键结果 → 构效关系）。
- **自带目录层级、自成一套的**（像 QFLS）：照 `app/qfls/` 的样子做一条独立流水线（扫描 → 校验 → 指标 → 独立输出目录 → 页面），
  不进数据库。以后要和主线打通，在输出目录之上加一步「把关键指标写进 key_result」即可（样品身份怎么对上要先和用户定）。

---

## 8. 测试与验收

| 层 | 怎么跑 | 管什么 |
|---|---|---|
| 单元 / 接口测试 | `.venv/bin/python -m pytest -q`（约 1 分钟） | 每个测试一个临时工作区（`tests/conftest.py` 的 `workspace` fixture）；模型调用用 `httpx.MockTransport`，不连外网 |
| 静态检查 | `.venv/bin/ruff check app tests tools` | 目标 Python 3.11（**3.12 才有的 f-string 嵌套引号会被拦**） |
| 守护测试 | 包含在 pytest 里 | 路由不许 `async`（`test_api.py`）；可点元素要能用键盘（`test_frontend_a11y.py`）；前端无环依赖（`test_frontend_bundle.py`）；`ops.js` ↔ `ops.py` 逐点一致（`test_ops.py`，需要 node）；启动脚本版本范围四处一致（`test_launchers.py`）；参考手册是最新的（`test_reference_doc.py`）；QFLS 演示数据的已知答案全部找到 |
| 浏览器验收 | `tools/qa/*_check.js`（Playwright），准备步骤见 `tools/qa/README.md` | 每条检查输出一个**量出来的数**（不是「看起来没问题」）；11 组：A2、B2、B3、L、E、D、网关、智能体、读写文件与批处理、权限与命令、QFLS |
| CI | GitHub Actions（`.github/workflows/ci.yml`） | ruff；Linux 3.11 / 3.13 + Windows 3.11 / 3.15 跑 pytest；**真跑一次 run.bat / run.sh** |

浏览器验收要一个起好的平台（`HTE_WORKSPACE=/tmp/hte-qa HTE_PORT=8799 HTE_NO_BROWSER=1 .venv/bin/python -m app.main`）
和假网关（`python3 tools/qa/fake_gateway.py 8797`，认得测试剧本，不用真模型）。QFLS 那组要一个**空的**新工作区。

**Windows 上的坑**：Python 3.11 的默认编码是 cp1252 —— 测试里写中文文件一定要 `encoding="utf-8"`（C064 就是这么红的）。

---

## 9. 记录体系（`docs/plan/`）

| 东西 | 在哪 | 什么时候写 |
|---|---|---|
| 逐次修改报告 | `docs/plan/changes/C###_日期_本提交.md` | **每个提交**；模板在 `CLAUDE.md` |
| 阶段报告 | `docs/plan/phases/P##_日期_主题.md` | 用户一轮需求做完 |
| 总计划（给用户看） | `docs/plan/index.html` | 每次提交加附录一行；新 P / D 进 §3 / §4；阶段末更新头部数字、§2、§5、§7 |
| 用户指定的 | `index.html` §3，编号 `P-##` | 写清出处（尽量引原话） |
| 我自行决定的 | `index.html` §4，编号 `D-##` | 影响数值 / 结论的 `data-confirm="1"` + 「建议你确认」 |

写这份文档时（P18 结束）：C001 – C072（下一个是 **C073**）、P-01 … P-58、D-01 … D-187（**27 项待用户确认**，D-02 最要紧）、阶段到 **P18**。
编号以 `docs/plan/changes/` 里最大的号和 `index.html` 的登记表为准 —— 这句话会过期，那两处不会。
历史报告（`changes/`、`phases/`）**不改** —— 它们是当时的记录。

---

## 10. 踩过的坑（别再踩一次）

| 坑 | 后果 | 现在的做法 |
|---|---|---|
| 路由写成 `async def` | 阻塞活卡住事件循环，进度条轮询全停 | 一律普通 `def`，测试拦 |
| 页面异步回来不查 `alive()` | 导航渲染两次、离开后轮询改了别页标题、旧响应盖新状态 | 页面生命周期规则（§6.3） |
| 一轮里先造几张图再挂进页面 | `trackWidth` 把先造的那张的宽度观察者误断，图停在 760px（C066） | 刚造的不断、拿掉的当场断 |
| `confirmDialog` 的 onClose 先 resolve(false) | 「确定」永远等于「取消」，装模块、删对话从界面上点不动（C054） | 答案只在 onClose 里交一次 |
| 弹窗套弹窗按 Esc | 两个一起关 | `modalStack`，只关最上面那个 |
| 智能体改 UTF-8 文件 | 凭空加 BOM（C059） | 保持原编码；认不出的不按文本改 |
| 关键结果没有「当前值」 | 跑两次批处理，AI 拿到旧值 | `key_result_current` 视图 |
| 缓存只按内容寻址 | 解析器修了 bug，旧缓存照样命中 | 缓存文件名带 `PARSER_VERSION` |
| 「是不是矩阵」三份判断 | 时间点少的原位文件被判错 | 只有 `matrix.probe()` 一份 |
| 预设里写了像密钥的说明 | 守护测试红 | 预设只放地址 |
| 测试写中文不指定编码 | Windows / 3.11 上 cp1252 写不进去 | 一律 `encoding="utf-8"` |
| 3.12 才有的 f-string 写法 | CI 的 3.11 语法错误 | ruff 的目标版本是 3.11 |
| 批处理列表默认只给 20 条 | 验收脚本数「多了一次」数不出来 | 要全量时显式 `limit` |
| Moran's I 逐像素算 | 光学模糊让相邻像素天然相关，I 永远接近 1 | 在 32 × 48 块网格上算（D-170） |
| matplotlib 默认没有中文字体 | 图里中文变方框 | 图里一律英文 + 编号，对照表在网页 / summary.json |
| 切页用 View Transitions、弹窗背景加模糊 | 实测 p95 帧时间 17 → 67–100 ms（C078） | 只让新页进场；遮罩只变暗不模糊 |
| 「当前值」视图全改成 GROUP BY 写法 | 整表汇总快 20 倍，但放进逐样品 EXISTS 子查询里 9 秒 | 两个视图各用各的（`key_result_current` 逐样品、`_all` 整表） |
| 用 `awk length` 数行长 | 按字节数，一个汉字算 3 —— 误报一大片 | 用 Python 按字符数（测试就是这么数的） |
| 模块结果缓存只按版本号失效 | 同事改了代码没改版本号，拿到旧结果 | 模块一重新加载就整个清空 |
| 弹窗关的时候还叫 `.modal` | 退场那 150 ms 里，键盘、测试脚本、再开一个会碰到它 | 关的那一刻改名 `.modal-leaving` |

---

## 11. 现状、限制、下一步

**能用的**（详见 `docs/plan/index.html` §2）：导入（子文件夹 = 一次测量、去重、命名规则）、三维筛选与样品集、样品页三个处理
（光谱、膜厚、特殊处理）、功能模块（A / B 档、zip 分发、命令行检查）、批处理与对比（叠图、分位数带、时刻切片、导出 plot.py）、
对比历史、关键结果审核、断链巡检、构效关系第一版（散点 + 相关 + 箱线图）、AI 抽屉、ⓘ 浮层、智能体（读写文件、跑命令、批处理、
任务、询问 / 自动、计划 / 直接执行）、模型网关（两种协议、重试、降级、测速）、**QFLS 成像**（扫描、校验、指标、评分、五个界面、导出）。

**没验证过 / 有边界的**：
- **QFLS 只在假想数据上验过**；qvdf 真实格式未知；10 项阈值待用户确认（D-162、D-165 … D-173）
- AI 助手和智能体**没在用户的真实网关上试过**（内设网关 `http://newapi.catl.com/v1` 已进预设）
- Windows 实机清单（`docs/WINDOWS_实机清单.md`）等用户或同事跑一次
- 智能体跑命令**不是沙箱**（以用户身份运行，只能事后发现动了平台核心）
- 万级样品：批处理流式写表、分页等还没做（§7 K 组）

### 11.1 技术债（P18 体检列出的那张清单，P19 全部清掉了）

P18 体检时列了后端 10 条、前端 8 条，P19（C073 – C077）一条条改完，每条对应的提交：

| 原来的问题 | 改在 |
|---|---|
| 原子写 4 份、防公式注入放错地方、编码探测 3 份；存密钥的配置反而不是原子写 | C073 `app/util/` |
| 时间两种写法、id 三种格式 | C073 `app/util/ids.py`（一律 UTC；可排序 id `前缀_时间_hex6`） |
| `guard()` 把所有 KeyError 都翻成 404，程序 bug 显示成「没有这个」 | C073 `app/errors.py` NotFound / Invalid |
| 路由层里有业务逻辑（时间窗切片、模块 zip、SQL、AI 提示词） | C074 `batch_views.py` / `modules/install.py` / `storage/catalog.py` / `ai/assistant.py` |
| 路由之间互相 import 私有函数 | C074 `api/streaming.py`、`artifacts.load_matrix` |
| 批处理计算线程里读库；落库出错要等剩下的全部算完 | C074 |
| 上传没上限；批量标质量不去重、没上限 | C074（512 MB、500 条） |
| 后台任务没有中断收尾钩子 | C074 `tasks.register(kind, on_interrupted=…)` |
| QFLS 分数写死七项、箱线图画哪几个写死 | C074 `PARAMS["score"]` 驱动、`metrics.BOX_FIGURE` |
| 请求体全是 dict，`/api/docs` 看不出参数 | C075 `api/bodies.py`（拼错的键回 400） |
| 指标卡 4 份、字段表 3 份、下拉 2 份、分段控件 2 份不认方向键 | C076 `ui.js` / `field-cells.js` |
| 智能体页报错用提醒框；`busy` 重名；设置页靠全局事件；`api.get` 不收 signal；一百多行超长 | C076 |
| AI 卡片发起的批处理留在本页轮询（两条路） | C077 |
| 下钻页的返回写死、位置不一 | C077 `app.js` backOf |
| 总览最近的处理点不进去；对比历史不能删改；批处理结果的样品表点不进去；地址栏停在 task_ | C077 |

**还剩的、有意没动的**
- 响应体没有模型（返回结构见路由 docstring 和 `docs/REFERENCE.md`）
- `api.with(signal)` 接在了加载最重的几页（总览、存储、对比历史、批处理结果）；样品页的光谱帧、QFLS 页还是「回来再丢掉」
- 样品页第一次进要 0.5 s 左右（870 KB 光谱帧 + 第一次膜厚计算）；要更快得改加载顺序（先低分辨率再补全），没做
- 删一次对比不能撤销（D-199 待确认；要「回收站」再说）

**建议的下一步**（用户还没定，别擅自开工大块）：
1. 拿到真实 QFLS 文件夹 → 校阈值、确认 qvdf 格式、补「把 QFLS 指标写进关键结果」（要先和用户定样品身份怎么对上）
2. 构效关系第二步（§7 E2）：异常识别 + 模型总结
3. 光谱处理迁成功能模块（§7 F）
4. 荧光 / 散射等新数据类型（§7 G，需要用户先给数据样例和算法规范）

---

## 12. 开工清单（新会话的前十分钟）

```bash
# 1. 装环境（第一次）
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt --only-binary=:all:
# 2. 测试全绿再动手
.venv/bin/python -m pytest -q
# 3. 起一个隔离的工作区看看界面（不碰真数据）
HTE_WORKSPACE=/tmp/hte-try HTE_NO_BROWSER=1 .venv/bin/python -m app.main
#    浏览器开 http://127.0.0.1:8765 → 「QFLS 成像」→「生成演示数据并处理」；样例光谱数据：sample_data/make_insitu.py
# 4. 看现在到哪了：docs/plan/index.html 的头部、「等你定」、§4 里「还待你确认」
```

然后：读用户这一轮的要求 → 拿不准的先问（或标「推断」）→ 改 → 测 → 写报告 + 登记 → 提交 → 推送 → 看 CI。
