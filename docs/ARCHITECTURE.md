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

没有前端构建链、没有 Node、没有 Electron。用户机器只需要 Python 3.11+。

## 后端模块

```
app/
├─ main.py           应用装配、静态挂载、端口自动避让
├─ config.py         工作区路径（可用 HTE_WORKSPACE 搬走）
├─ api/              路由。错误统一翻译成 {error:{message,kind,detail}}
├─ storage/
│   ├─ schema.sql    数据模型
│   ├─ db.py         连接（按线程缓存）、WAL、设置读写
│   ├─ ingest.py     ★ 分类落地策略、sha256 去重、断链巡检
│   ├─ naming.py     ★ 文件名 → sample_id
│   ├─ artifacts.py  artifact 查询、local_path() 吃掉复制/引用差异
│   ├─ results.py    analysis_run + key_result 读写
│   └─ tabular.py    Parquet 读写
├─ skills/
│   ├─ base.py       ★ Skill 契约（见 docs/SKILL_CONTRACT.md）
│   ├─ registry.py   发现、注册、推荐打分
│   ├─ runner.py     ★ 处理与存储之间唯一的桥
│   ├─ adapters/     python_skill.py（主通道）+ skillmd.py（模型通道）
│   └─ builtin/      table_preview（可运行）+ thickness / spectrum（待接入）
├─ ai/
│   ├─ provider.py       AIProvider 抽象 + NullProvider
│   ├─ openai_compat.py  OpenAI 兼容多 provider 客户端
│   └─ rules.py      ★ 确定性规则引擎 —— 没有模型时它就是全部
└─ parsers/sniff.py  编码 / 分隔符 / 抬头 / 列类型嗅探
```

## 三条设计原则

**1. 原始数据永不被修改。**
复制是只读复制，引用是只读引用。断链要显式标 `missing`，不静默失败。

**2. 没有模型，平台也要完整可用。**
文件识别、skill 推荐、数据质量检查全部走确定性规则引擎。
模型只做它真正擅长的：解释、总结、从非结构化文本里抽字段。
界面上每条结论都标注来源是「规则」还是具体模型名 —— 不制造假的智能感。

**3. 加一个 skill，界面免费出现。**
`spec.params` → 表单，`spec.outputs` → 结果卡片，`preview` → 图表。
skill 作者不需要碰任何前端代码。这是「可以丰富的架子」的具体含义。

## 前端

```
web/
├─ index.html
├─ css/   tokens（唯一定义颜色的地方）· base · layout · components
└─ js/
    ├─ ui.js       h() DOM 助手、toast、modal、空/加载/错误三态
    ├─ api.js      fetch 封装，错误结构统一
    ├─ chart.js    自写 SVG 图表（框选缩放、悬停读数、明暗自适应）
    ├─ app.js      导航、主题、健康检查
    ├─ components/ form.js（ParamSpec → 表单）· filepicker.js（服务端目录浏览）
    └─ pages/      overview · process · storage · relation · settings
```

原生 ES Module，不打包。改一行刷新即可。

**为什么有服务端的目录浏览**：浏览器出于安全不会把文件真实路径给网页，
而「图像不复制、原地引用」必须知道真实路径。所以主通道是在应用里浏览本机目录。
拖拽上传仍然可用，但那条通道会把文件全部复制 —— 界面上写明了这一点。

## 安全边界

- 服务只监听 `127.0.0.1`，不对外
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
| 光学厚度 vs 时间 | ~600 点 | 前端 SVG | 后端（算法待接入） |

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
   （`web/js/spectra.js` ↔ `app/parsers/render.py`）。
2. **降采样必须用面积平均，不能抽点。** 条纹是高频信号，直接抽点会产生摩尔纹
   假象 —— 那是数据里根本不存在的图案。
3. **色标的亮度必须单调。** 否则数值不同的区域看起来一样亮，人眼直接读错。
   `tests/test_spectra.py` 里有一条测试守着这件事。

### 缓存

解析一个 5.7 MB 的 CSV 要 890 ms，解析结果缓存成 npz 后读回只要 20 ms。
缓存放 `workspace/cache/matrix/<sha256>.npz` —— 内容寻址意味着**文件改了 sha 就变了，
旧缓存自然不会被命中**，不需要写任何失效逻辑。整个 cache 目录随时可以删，
代价只是下次慢一点。

