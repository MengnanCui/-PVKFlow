# 架构

## 三层，时间上有先后

```
数据处理  →  数据存储  →  构效关系
（本期重点）  （本期重点）   （留架子）
```

先有数据处理，然后是存储，最后才谈得上构效关系分析。
这一期把前两层做扎实，第三层只留入口和数据形态。

## 进程结构

```
run.bat / run.sh
   └─ python -m app.main
        ├─ uvicorn（127.0.0.1:8765，只监听本机）
        ├─ FastAPI
        │    ├─ /api/*        —— JSON 接口
        │    └─ /assets/*     —— 静态前端（零构建）
        └─ 自动打开浏览器
```

没有前端构建链、没有 Node、没有 Electron。用户机器只需要 **Python 3.11 – 3.15**
（依赖只在这几个版本上都有 Windows 预编译包；`run.bat` / `run.sh` 从新到旧挑范围内的那个，
装依赖只装预编译包 —— 宁可马上报错，也不在没有编译器的电脑上卡死。范围写在四处，`tests/test_launchers.py` 守着它们一致）。

## 后端模块

```
app/
├─ main.py           应用装配、静态挂载、端口被占时往后找
├─ config.py         工作区路径（可用 HTE_WORKSPACE 搬走）
├─ security.py       只认本机 Host、只认自己页面的 Origin（见「安全边界」）
├─ tasks.py          后台任务：一次跑一个（批处理），进度 / 取消 / 重启后收尾
├─ batch.py          ★ 批处理：配方 → 每个样品跑一遍各模块 → 长表 + 关键结果
├─ api/              路由。错误统一翻译成 {error:{message,kind,detail}}；**全部是普通函数**（放线程池，见下）
├─ storage/
│   ├─ schema.sql    数据模型（13 张表 + 1 个视图，见 DATA_MODEL.md）
│   ├─ migrations.py ★ 版本号 + 迁移；升级前先备份（见下）
│   ├─ db.py         连接（按线程缓存）、WAL、设置读写
│   ├─ ingest.py     ★ 分类落地策略、sha256 去重、断链巡检、导入时判定是不是光谱矩阵
│   ├─ naming.py     ★ 文件夹名 / 文件名 → 样品、批次、测量时间
│   ├─ artifacts.py  artifact 查询、local_path() 吃掉复制/引用差异
│   ├─ selection.py  筛选式 → SQL（一次选择是一个筛选式，不是一串 ID）
│   ├─ results.py    analysis_run + key_result 读写、字段列表
│   ├─ sets.py       保存的样品集
│   ├─ conversations.py  AI 对话与钉住的分析
│   └─ tabular.py    长表读写（CSV；装了 pyarrow 则 Parquet）
├─ modules/          ★ 功能模块（同事写算法的唯一入口，见 docs/MODULE_AUTHORING.md）
│   ├─ base.py       契约：ModuleSpec / Control / Panel / Curve / Op
│   ├─ ops.py        A 档实时算子（与 web/js/ops.js 逐点一致，tests/test_ops.py 守着）
│   ├─ registry.py   发现、加载（先装到新容器再一次换上）、模块目录
│   ├─ validate.py   装之前的验证 + 试跑
│   ├─ check.py      命令行验证：python -m app.modules.check <目录>
│   └─ builtin/      thickness（膜厚）· special_processing（谱斜率 / 波段积分）
├─ analysis/fringe_ot.py  膜厚算法（DEFAULTS 是冻结规范的原文，不改；平台用 override）
├─ parsers/
│   ├─ sniff.py      编码 / 分隔符 / 抬头 / 列类型嗅探
│   ├─ insitu_csv.py 原位 Data.csv（认结构，不认名字）
│   ├─ matrix.py     ★ 光谱矩阵：解析、缓存、`probe()`（「是不是矩阵」只有这一份判断）
│   └─ render.py     热力图 / 条纹图 PNG
├─ plotting/script_export.py  导出一份能自己跑的 plot.py + data.csv
└─ ai/
    ├─ provider.py       AIProvider 抽象
    ├─ openai_compat.py  OpenAI 兼容多 provider 客户端
    └─ context.py        给模型的「已知事实」：汇总、逐样品标量、曲线摘要
```

## 三条设计原则

**1. 原始数据永不被修改。**
复制是只读复制，引用是只读引用。断链要显式标 `missing`，不静默失败。

**2. 没有模型，平台也要完整可用。**
导入、处理、批处理、导出都不依赖模型。模型只出现在 AI 抽屉和 ⓘ 浮层里：
解释、总结、提议收窄范围 —— 提议的筛选式要变成你看得见、能改、能拒绝的卡片，跑不跑由你点。
（原来还有一个确定性的规则引擎负责「文件识别、skill 推荐」，随旧 Skill 机制一起退役了，2026-11。）

**3. 加一个模块，界面免费出现。**
模块只交算法和声明：`controls` → 控件，`panels` → 图，`batch_curves` → 对比页的曲线。
同事不需要碰任何前端代码。A 档面板（声明成平台算子的）拖控件时在浏览器里实时算，
B 档（自己写 `compute()` 的）松手后由后端算。

### 路由一律是普通函数

这个平台的路由全是阻塞的活（解析文件、跑模块、写库）。写成 `async def` 就直接跑在服务的事件循环里，
期间别的请求 —— 包括界面上的进度条轮询 —— 全得等（2026-11 上传和装模块就是这样）。
普通函数 FastAPI 会放进线程池。`tests/test_api.py` 里有一条测试不许任何路由是 `async`。

流式回答（AI）是一个生成器，用的是 `chat._ClosingStream`：流一结束（说完了、或者你点了停止 / 关了页面）
就亲手关掉生成器，让它把已经说出来的字存下 —— Starlette 发现断开后只是不再往下取，并不关它。

### 数据库版本与迁移

版本号存在 SQLite 的 `PRAGMA user_version`，每一步另记一行 `schema_migration`。
已有数据的工作区**升级前先备份** `hte.db` 到 `workspace/backups/`（只留最近 5 份）；全新工作区直接建到最新。
加一步迁移 = 在 `migrations.MIGRATIONS` 末尾追加，**已发出的迁移不改、每一步都能重跑**。目前到第 2 步：

| 版本 | 内容 |
|---|---|
| 1 | 补列：`analysis_run.parent_run_id`、`artifact.is_matrix` |
| 2 | `measurement.measured_at` 统一成本地时间（原来退回文件修改时间的那些存的是 UTC） |

## 前端

```
web/
├─ index.html
├─ css/   tokens（唯一定义颜色的地方）· base · layout · components
└─ js/
    ├─ ui.js       h() DOM 助手、toast（aria-live）、modal（dialog、焦点困住 / 归还）、空/加载/错误三态、latest()
    ├─ api.js      fetch 封装，错误结构统一
    ├─ chart.js    自写 SVG 图表（框选缩放、悬停读数、明暗自适应）
    ├─ ops.js      A 档实时算子（与 app/modules/ops.py 逐点一致）
    ├─ spectra.js  选时刻叠谱、认出饱和的短波端
    ├─ csv.js      图表数据 → CSV（不碰 DOM；文字格子防公式注入）
    ├─ download.js 「下载 ▾」：图片 / 数据
    ├─ app.js      导航、主题、健康检查
    ├─ components/ ai-drawer · info（ⓘ 浮层）· chat-client（两者共用：模型选择 + 流式发问）
    │              figure · heatmap · module-panel · facets · filepicker · virtual-list · field-cells
    └─ pages/      overview · process(+4) · sample · batch(+4) · history · storage · relation · settings
```

`batch.js`、`process.js` 原来各一千行上下，2026-11 按页面上的块拆开（`batch-state / -recipe / -slices / -curves`、
`process-state / -filter / -select / -run`），每个文件开头写了自己管什么。依赖必须**无环**（离线烤制工具的要求）：
拆出去的文件要回头调页面入口的函数时，经 `*-state.js` 里的 `hooks` 转一道。

原生 ES Module，不打包。改一行刷新即可。

### 页面的生命周期（写页面代码必须遵守）

页面是常驻的（切走只是隐藏），所有页面状态放在模块级变量里。所以**异步的东西回来时，
你可能已经不在这一页了，甚至已经换成同一页的另一个对象**。2026-11 的自查里，
这一条造成了一串问题：导航渲染两次、拖拽监听越挂越多、离开对比页后轮询照跑并改掉
别页标题、快速操作时旧响应覆盖新状态。规则：

- `view(host, ctx)` 拿到的 `ctx` 里有 `signal`（离开即 abort）和 `alive()`。
  **每个 `await` 之后、改模块级状态或往页面 / 标题栏写东西之前，先 `if (!alive()) return;`**
- 轮询、定时器、挂在页面外面的监听（滚动、ResizeObserver）都要在离开时停：
  轮询每轮查 `alive()`，监听挂 `ctx.signal.addEventListener('abort', 摘掉)`
- 会被连续触发的请求（筛选、搜索、拖控件、目录浏览）用 `ui.js` 的 `latest()`：
  发请求前取号，回来后 `if (!fresh()) return;` —— 只认最后一次
- 往同一个常驻节点上挂监听要防重复（见 `filepicker.bindDropUpload`）

浏览器验收脚本在 `tools/qa/`，改完前端跑一遍。

**为什么有服务端的目录浏览**：浏览器出于安全不会把文件真实路径给网页，
而「图像不复制、原地引用」必须知道真实路径。所以主通道是在应用里浏览本机目录。
拖拽上传仍然可用，但那条通道会把文件全部复制 —— 界面上写明了这一点。

## 安全边界

- 服务只监听 `127.0.0.1`，不对外
- 接口只认本机地址、只认平台自己的页面（`app/security.py`）：
  Host 不是本机名字的一律 403（防 DNS 重绑定读本机文件）；
  `/api/` 下带了外站 Origin、或浏览器标了跨站的请求一律 403（防别的网页借你的浏览器改设置、装模块）
- 模块是可执行的 Python，平台不做沙箱；设置页导入 zip 之前先弹确认
- 目录浏览接口是只读的
- 模型密钥存 `workspace/config/providers.json`（在 `.gitignore` 里），
  接口返回时一律打码，前端拿不到完整密钥

## 图怎么画：位图归位图，曲线归曲线

「吸收光谱 & 膜厚」这类原位数据是一个 波长 × 时间 的矩阵（典型 1501×601 ≈ 3.6 MB）。
怎么把它画到屏幕上，不是一个"要不要实时"的问题，而是**这张图的输出是位图还是曲线**。

| 视图 | 数据量 | 谁画 | 换参数时 |
|---|---|---|---|
| 波长–时间热力图 | 10⁶ 像素 | 服务端 PNG | 后端重出 ~90 ms |
| 波数–时间条纹图 | 10⁶ 像素 | 服务端 PNG | 后端重出 ~30 ms |
| 归一化强度 vs 波长 | 20 条 × 400 点 | 前端 SVG | **前端 0 ms** |
| 谱斜率 / 波段积分 vs 时间 | ~600 点 | 前端 SVG | **前端 0.1 ms** |
| 光学厚度 vs 时间 | ~600 点 | 前端 SVG | 后端（`analysis/fringe_ot.py`） |

**热力图做成 PNG 不是偷懒。** 10⁶ 个 SVG 矩形会卡死浏览器；用 Canvas 画则要把
3.6–25 MB 的矩阵传到前端，而同一张图 PNG 只要 0.15–0.35 MB。反过来，把曲线做成
死图是真亏 —— 丢掉悬停读数、框选放大、导出、明暗主题自适应，只省几十 KB。

### 中间数据集：一份数据喂三张图

处理时把光谱抽样成一份"中间数据集"（自适应决定波长步长，控制在约 1 MB），
前端一次拿走。之后：

* 换叠谱时刻 → 本地切列
* 换积分波段 → 本地梯形积分，0.16 ms
* 换斜率波长 → 本地最小二乘，0.03 ms

拖滑块时曲线连续跟着变；**停手约 0.3 秒后自动向后端取全波长分辨率的精确结果**
并替换，界面上如实标注当前看到的是"实时预览"还是"全分辨率"。抽样带来的偏差
实测为信号 RMS 的 0.02%（积分）到 1.6%（斜率）。

传输走二进制而不是 JSON：同一份 376×601 的抽样谱，JSON 要 6.7 MB，
float32 只要 0.90 MB，本地 37 ms 到手。

### 三条硬约束

1. **膜厚的 FFT 提取只在后端做。** 同一段物理有两份实现，一旦漂移就分不清该信
   哪个数。前端只做那些能和后端逐行对照、并且有测试交叉验证的简单算术
   （`web/js/ops.js` ↔ `app/modules/ops.py`，`tests/test_ops.py` 在 node 里逐点比对）。
2. **降采样必须用面积平均，不能抽点。** 条纹是高频信号，直接抽点会产生摩尔纹
   假象 —— 那是数据里根本不存在的图案。
3. **色标的亮度必须单调。** 否则数值不同的区域看起来一样亮，人眼直接读错。
   `tests/test_spectra.py` 里有一条测试守着这件事。

### 缓存

解析一个 5.7 MB 的 CSV 要 890 ms，解析结果缓存成 npz 后读回只要 20 ms。
缓存放 `workspace/cache/matrix/<sha256>.p<解析器版本>.npz` —— 内容寻址意味着**文件改了 sha 就变了，
旧缓存自然不会被命中**。但光靠内容寻址不够：**解析器本身改了**（比如修了一个读错列的 bug），
文件没变、sha 没变，旧缓存里存的还是读错的结果。所以文件名里还带着 `matrix.PARSER_VERSION`，
改解析逻辑时把它加一，旧缓存就全部作废（有测试守着）。整个 cache 目录随时可以删，代价只是下次慢一点。

### 「是不是光谱矩阵」

只有一份判断：`matrix.probe()`，只读文件头（原位 Data.csv 看前 512 KB、认出来就停）。
导入时判一次写进 `artifact.is_matrix`；列样品时碰到还没判过的老数据，惰性补判，用的是同一个函数。
原来是三份规则不一样的判断（惰性那份不认原位格式，时间点少的原位文件会被判成不是矩阵），2026-11 合成一份。

