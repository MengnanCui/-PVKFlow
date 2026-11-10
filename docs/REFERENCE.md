# 参考手册（自动生成，不要手改）

> 由 `tools/gen_reference.py` 从代码生成；`tests/test_reference_doc.py` 检查它是不是最新的 —— 改了接口、页面、工具、表、
> 环境变量或 QFLS 阈值之后跑一下 `.venv/bin/python tools/gen_reference.py`。讲设计思路的是 `docs/HANDOFF.md`。

## 1. 菜单与页面

| 侧栏分组 | 页面 id（地址栏 `#id/参数`） | 标题 | 说明 | 文件 |
|---|---|---|---|---|
| 平台 | `overview` | 总览 | 处理 → 存储 → 构效关系。当前阶段的重点是前两步。 | `web/js/pages/overview.js` |
| 平台 | `process` | 数据处理 | 选一个功能，筛出一批样品 | `web/js/pages/process.js` |
| 平台 | `relation` | 构效关系 | 任选两个字段看相关，按批次看分布 | `web/js/pages/relation.js` |
| 平台 | `storage` | 数据存储 | 全部数据在这里。数据处理页是挑一批出来跑，这里是查它们都存到哪儿了 | `web/js/pages/storage.js` |
| 分析 | `agent` | 智能体 | 查数据、读写文件、跑命令和批处理 —— 写的东西都在专用目录里，会改东西的操作按你的设置先问 | `web/js/pages/agent.js` |
| 分析 | `history` | 对比历史 | 每一次批处理都留在这里，随时点回去 | `web/js/pages/history.js` |
| 分析 | `qfls` | QFLS 成像 | 准费米能级分裂成像：逐点校验、指标、评分、排名、对比 | `web/js/pages/qfls.js` |
| 系统 | `settings` | 设置 | 导入策略、命名规则、模型接入 | `web/js/pages/settings.js` |
| 下钻页（从「history」进入） | `batch` | 批处理结果 |  | `web/js/pages/batch.js` |
| 下钻页（从「qfls」进入） | `qfls-point` | QFLS 样点 | 一个样点的全部指标、分数来由、异常和图 | `web/js/pages/qfls-point.js` |
| 下钻页（从「process」进入） | `sample` | 样品 |  | `web/js/pages/sample.js` |

## 2. 接口

共 109 个接口。全部是本机接口（只认 127.0.0.1 / localhost，见 `app/security.py`）；错误一律是 `{"error": {"message", "kind", "detail"}}`。交互式文档：启动后打开 `/api/docs`。

### agent（`app/api/agent.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/agent/file-changes` | session_id |  |  |
| POST | `/api/agent/file-changes/{change_id}/rollback` |  | 有 | 界面上「撤销这次改动」—— 是你点的，不经过模型、也不用再批。 |
| POST | `/api/agent/run` |  | 有 | 说一句话。没有 session_id 就新建一个 —— 等第一句话才建，不攒空会话（L-1 的教训）。 |
| GET | `/api/agent/sessions` |  |  |  |
| DELETE | `/api/agent/sessions/{session_id}` |  |  |  |
| GET | `/api/agent/sessions/{session_id}` |  |  |  |
| POST | `/api/agent/sessions/{session_id}/resume` |  | 有 | 批完接着跑：{decisions: {调用id: approve\|reject}, remember: [工具名]}。没提到的调用当作拒绝。 |
| POST | `/api/agent/sessions/{session_id}/stop` |  |  | 「停止」：回路在下一步之间、等任务的下一秒就停。前端点停止时先调它再断开连接。 |
| POST | `/api/agent/sessions/{session_id}/undo-files` |  |  | 「/undo」：撤销这个会话里智能体改过的全部文件（从新到旧）。是你点的，不经过模型。 |
| GET | `/api/agent/settings` |  |  |  |
| POST | `/api/agent/settings` |  | 有 |  |
| GET | `/api/agent/tools` |  |  |  |

### artifacts（`app/api/artifacts.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/artifacts/{artifact_id}/head` | lines |  | 看文件前几行 —— 判断分隔符/抬头对不对时最直接的办法。 |
| GET | `/api/artifacts/{artifact_id}/preview` | rows |  | 把文件解析成表格预览。前端表格与图表都吃这个。 |
| GET | `/api/artifacts/{artifact_id}/raw` |  |  |  |
| GET | `/api/artifacts/{artifact_id}/thumb` |  |  |  |

### assist（`app/api/assist.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/assist/status` |  |  | 界面据此决定 AI 能不能用、显示哪个模型。 |

### batch（`app/api/batch.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/batch/preview` |  | 有 | 跑之前先看会处理多少个、有多少个没有矩阵。 |
| POST | `/api/batch/run` |  | 有 | 提交一次批处理。 |
| GET | `/api/batch/runs` | limit |  |  |
| GET | `/api/batch/runs/{parent_run_id}` |  |  |  |
| GET | `/api/batch/runs/{parent_run_id}/axes` |  |  | 这批数据的波长轴和时间轴范围（读不到返回 available=false，不报错）。 |
| GET | `/api/batch/runs/{parent_run_id}/curves` | column, max_series |  | 批处理的曲线，按样品分组返回（最多 max_series 条，并说明截断了多少）。 |
| GET | `/api/batch/runs/{parent_run_id}/export` | column, mode, group_by, max_series |  | 打包 plot.py + data.csv + README.md 下载 —— 平台内置图型不够用时，拿脚本走人随便改。 |
| GET | `/api/batch/runs/{parent_run_id}/export/preview` | column, mode, group_by |  | 先读脚本，再决定要不要下载。 |
| GET | `/api/batch/runs/{parent_run_id}/slices` | windows |  | 不同样品在若干个时间窗内的平均膜厚（口径见 app/batch_views.slices）。 |

### chat（`app/api/chat.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/chat/conversations` | limit, topic |  | 带 `topic` 就只列这个术语下的对话（术语 ⓘ 弹窗用），不带就是全部。 |
| POST | `/api/chat/conversations` |  | 有 |  |
| DELETE | `/api/chat/conversations/{conversation_id}` |  |  |  |
| GET | `/api/chat/conversations/{conversation_id}` |  |  |  |
| PATCH | `/api/chat/conversations/{conversation_id}` |  | 有 |  |
| POST | `/api/chat/conversations/{conversation_id}/messages` |  | 有 |  |
| GET | `/api/chat/pins` | run |  |  |
| POST | `/api/chat/pins` |  | 有 |  |
| DELETE | `/api/chat/pins/{pin_id}` |  |  |  |
| POST | `/api/chat/scope/preview` |  | 有 | 抽屉里那个范围选择器的数字从这儿来。 |

### files（`app/api/files.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/files` | q, sample_id, batch_id, ext, status, limit, offset |  |  |
| GET | `/api/files/browse` | path, show_hidden |  | 列一个目录。只读，不改任何东西。 |
| GET | `/api/files/facets` |  |  |  |
| POST | `/api/files/import` |  | 有 | 把（可能被用户改过的）预览行写进库。 |
| POST | `/api/files/naming/preview` |  | 有 | 试一条命名规则，看它能不能正确解析出样品名。 |
| GET | `/api/files/roots` |  |  | 给目录浏览器一个起点：盘符（Windows）或常用目录。 |
| POST | `/api/files/scan` |  | 有 | 扫描 + 分类 + 样品匹配预览。不写任何东西，给人确认用。 |
| POST | `/api/files/upload` |  | 有 | 拖拽上传通道。 |
| POST | `/api/files/verify` |  |  | 巡检原始文件（引用型 + 复制型）还在不在；找不到的列出来（最多 200 条，总数照实给）。 |
| GET | `/api/files/{artifact_id}` |  |  |  |

### modules（`app/api/modules.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/modules` |  |  |  |
| POST | `/api/modules/import` |  | 有 | 装一个模块的 zip。**先验证，通过了才落地**；同 id 是升级，平台自带的不让覆盖。 |
| POST | `/api/modules/reload` |  |  |  |
| POST | `/api/modules/validate` |  | 有 | 验证一个已装的模块。装之前的验证走 /import。 |
| DELETE | `/api/modules/{module_id}` |  |  | 卸载（删它的目录、重载）。平台自带的不让卸。 |
| POST | `/api/modules/{module_id}/compute` |  | 有 | 拿全分辨率矩阵算一遍这个模块的所有面板。 |
| GET | `/api/modules/{module_id}/export` |  |  | 打包成 zip 发给别人。这就是「分发」的全部 —— 不需要 Git。 |

### qfls（`app/api/qfls.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/qfls/method` |  |  | 方法说明页要的：阈值、指标定义。和处理时用的是同一份 PARAMS。 |
| GET | `/api/qfls/runs` |  |  |  |
| POST | `/api/qfls/runs` |  | 有 |  |
| DELETE | `/api/qfls/runs/{run_id}` |  |  |  |
| GET | `/api/qfls/runs/{run_id}` |  |  |  |
| GET | `/api/qfls/runs/{run_id}/files/{path}` |  |  |  |
| GET | `/api/qfls/runs/{run_id}/points` |  |  |  |
| GET | `/api/qfls/runs/{run_id}/points/{key}` |  |  |  |
| POST | `/api/qfls/scan` |  | 有 | 只扫描不处理：认出了哪些实验、多少帧、跳过了什么。 |

### relation（`app/api/relation.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/relation/groups` |  | 有 | {y, by?, filter?} → 每组的箱线图统计，离群点带样品。 |
| POST | `/api/relation/pairs` |  | 有 | {x, y, filter?} → 每个样品一个点 + 皮尔逊 / 斯皮尔曼（带 p 值）。 |

### results（`app/api/results.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/overview` |  |  | 总览页的数字。全部来自数据库，为零就是零。 |
| GET | `/api/results` | field, sample_id, quality, limit, offset, history |  | 默认每个 (样品, 字段) 只给当前值；`?history=true` 给全部历史。 |
| GET | `/api/results/by-sample/{sample_id}` |  |  | 一个样品的全部关键结果：每个字段的当前值 + 历史。 |
| GET | `/api/results/fields` |  |  | 有哪些字段可选 —— 构效关系页选 X/Y 的候选来源。 |
| POST | `/api/results/quality` |  | 有 | 一次审核几条：{ids: [...], quality, note?}。一条不存在就一条都不改。 |
| POST | `/api/results/{result_id}/quality` |  | 有 | 审核一条：{quality: validated\|review\|reject, note?}。返回这条和审核之后的当前值。 |
| GET | `/api/runs/{run_id}` |  |  |  |
| GET | `/api/samples` | limit |  |  |
| GET | `/api/storage/stats` |  |  | 数据存储页要展示的真实分布。 |
| GET | `/api/tables/{table_id}` | limit |  |  |

### selection（`app/api/selection.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/selection/count` |  | 有 |  |
| POST | `/api/selection/facets` |  | 有 | 分面与计数。 |
| POST | `/api/selection/ids` |  | 有 | 展开成 ID 列表。只在真的要执行时用。 |
| POST | `/api/selection/query` |  | 有 | 按筛选式取一页样品。服务端分页 —— 千级列表不能整个推给前端。 |
| GET | `/api/selection/sets` |  |  |  |
| POST | `/api/selection/sets` |  | 有 |  |
| DELETE | `/api/selection/sets/{set_id}` |  |  | 只删样品集本身（名字 + 筛选式 / 快照），样品和结果不动。 |
| GET | `/api/selection/sets/{set_id}` |  |  |  |
| POST | `/api/selection/sets/{set_id}/freeze` |  |  | 把动态集在此刻钉死。要发论文了就用这个。 |
| POST | `/api/selection/sets/{set_id}/rename` |  | 有 |  |
| POST | `/api/selection/suggest` |  | 有 | 手选几个之后，提议扩展成一条规则。返回的是筛选式，不是 ID 列表。 |

### settings（`app/api/settings.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/settings` |  |  |  |
| POST | `/api/settings` |  | 有 |  |
| GET | `/api/settings/cache` |  |  | 解析缓存占了多少。上千个样品能到几个 GB，得让人看得见、能清掉。 |
| POST | `/api/settings/cache/clear` |  |  |  |
| GET | `/api/settings/models` |  |  | 模型配置概览。密钥在这里被打码，绝不原样回传前端。 |
| POST | `/api/settings/models` |  | 有 | 保存 providers 配置。 |
| POST | `/api/settings/models/discover` |  | 有 | 按地址拉一份可用模型列表，省得手打模型名。 |
| GET | `/api/settings/models/example` |  |  |  |
| DELETE | `/api/settings/models/presets` | base_url |  | 从本机名单里去掉一个地址。内置的那几条去不掉 —— 它们不在这个文件里。 |
| POST | `/api/settings/models/presets` |  | 有 | 把当前这个地址存成候选。 |
| POST | `/api/settings/models/simple` |  | 有 | 三个框存一个 provider：地址 + 密钥 + 模型名。 |
| POST | `/api/settings/models/test` |  | 有 |  |
| POST | `/api/settings/models/test-one` |  | 有 | 给一个模型打一句「回复 ok」，回延迟（照 IFlow 的 test-one）。界面上并发 3 个逐个测、边测边显示。 测不通也是 200 + ok:false —— 列表要把它沉底，而不是整体失败。 |

### spectra（`app/api/spectra.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/spectra/samples` |  |  | 带光谱矩阵的样品清单 —— 「吸收光谱 & 膜厚」的入口列表（查询在 app/storage/catalog.py）。 |
| GET | `/api/spectra/{artifact_id}/curve` | kind, lam_min, lam_max, center, half_width |  | 服务端版本的积分/斜率曲线。 |
| GET | `/api/spectra/{artifact_id}/frames` | lam_min, lam_max, max_time_points |  | 抽样光谱的**坐标轴与形状**，数值走 data_url 指向的二进制端点。 |
| GET | `/api/spectra/{artifact_id}/frames.bin` | lam_min, lam_max, max_time_points |  | 抽样谱的数值，float32 小端、行主序（行=波长）。配合 /frames 使用。 |
| GET | `/api/spectra/{artifact_id}/heatmap.png` | axis, lam_min, lam_max, norm, cmap, width, height |  | 热力图 / 条纹图。 |
| GET | `/api/spectra/{artifact_id}/meta` |  |  | 矩阵尺寸、采样率、值域，外加它属于哪个样品。界面上把这些如实显示出来 —— 用户第一次跑就知道自己的数据到底多大。 |
| GET | `/api/spectra/{artifact_id}/thickness` | lam_min, lam_max |  | 光学厚度 OT = n·d·cosθ vs 时间。 |

### tasks（`app/api/batch.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/tasks` | limit |  |  |
| GET | `/api/tasks/{task_id}` |  |  |  |
| POST | `/api/tasks/{task_id}/cancel` |  |  | 取消是「停在这儿」，不是「当没发生过」—— 已跑完的样品结果保留。 |

### 其它（`app/main.py`）

| 方法 | 路径 | 查询参数 | 请求体 | 说明 |
|---|---|---|---|---|
| GET | `/api/health` |  |  |  |

## 3. 后台任务种类

一次只跑一个（`app/tasks.py`），状态存在 `task` 表里，前端轮询 `/api/tasks/{id}`。

| 任务种类 | 执行函数 | 说明 |
|---|---|---|
| `batch.abs_thickness` | `app.batch.run_batch` | 任务执行体。参数：{filter, recipe, title} |
| `qfls_process` | `app.qfls.task.run_qfls` | 一次 QFLS 处理（演示数据时先生成）。参数：{root \| demo, full, run_id, title, figures, params} |

## 4. 智能体工具

共 25 个，定义在 `app/agent/tools.py` 的 `TOOLS`。

| 工具 | 风险 | 只在计划模式 | 说明 |
|---|---|---|---|
| `overview` | 只读（直接跑） |  | 平台里有多少样品、原始文件、关键结果，几条还待审核、几个文件找不到，最近几次批处理。问「现在有什么数据」时先调它。 |
| `list_fields` | 只读（直接跑） |  | 全部可分析的数值字段：字段名、中文名、单位、有几个样品有值、范围。不知道字段名（field_name）时先调它，别猜。 |
| `find_samples` | 只读（直接跑） |  | 按名字关键词 / 批次找样品，返回 sample_id、名字、批次、有几个结果。结果多时用 offset 翻页（返回里有 next_offset）。 |
| `sample_results` | 只读（直接跑） |  | 一个样品每个字段的当前值、审核状态（可信 / 待查 / 作废）、result_id，以及以前算过的几次。要审核某个结果时先用它拿 result_id。 |
| `correlate` | 只读（直接跑） |  | 两个字段在样品之间的相关：皮尔逊 r（含 p 值、95% 区间）、斯皮尔曼 ρ、拟合直线，以及离趋势最远的几个样品。数值一律以它算的为准，不要自己估。 |
| `compare_batches` | 只读（直接跑） |  | 一个字段按批次分组的分布：每批样品数、中位数、四分位、范围、离群的样品（带 result_id）。 |
| `list_runs` | 只读（直接跑） |  | 最近几次批处理：编号、标题、几个样品、几个失败、什么时候跑的。 |
| `list_sample_sets` | 只读（直接跑） |  | 已经存下的样品集：set_id、名字、动态 / 固定、几个样品。 |
| `check_files` | 只读（直接跑） |  | 查一遍原始文件还在不在记录的位置，找不到的列出文件名、样品、路径。只同步「找不到」标记，不改任何数据。 |
| `set_quality` | 会改东西（询问模式下先问，可记住） |  | 把一条或几条关键结果标为 validated（可信）/ review（待查）/ reject（作废），可附理由。作废后这个样品这个字段的当前值退回上一次。会先问用户。 |
| `save_sample_set` | 会改东西（询问模式下先问，可记住） |  | 把一批样品存成样品集。kind=pinned 钉死这些样品（给 sample_ids，或给 batch / q 按此刻命中的钉）；kind=dynamic 存筛选式（batch / q）。会先问用户。 |
| `delete_sample_set` | 不可逆（每次都问） |  | 删除一个样品集（只删集，不删样品）。删了找不回来，每次都会问用户。 |
| `list_files` | 只读（直接跑） |  | 列「能碰的文件夹」里的文件和子文件夹。path 相对工作区，或写名单里文件夹的完整路径；depth 1–4。不知道文件在哪先调它，别凭印象编路径。 |
| `read_file` | 只读（直接跑） |  | 读一个文本文件（带行号；UTF-8 / UTF-16 / GBK 都认）。大文件用 offset / limit 分段读，返回 next_offset 就是没读完。密钥会被打码。 |
| `search_files` | 只读（直接跑） |  | 在文件夹里按正则搜文件内容，返回 文件:行号:内容。glob 限定文件名，如 *.csv、**/Options.json。 |
| `write_file` | 会改东西（询问模式下先问，可记住） |  | 新建或修改文件：给 content = 整个文件；或给 find + replace = 把唯一出现的一段原文换掉（先 read_file 拿逐字原文）。每次写之前自动备份，返回 change_id，可以 rollback_file 撤销。工作区里的 raw / config / modules / 数据库是只读的。会… |
| `file_changes` | 只读（直接跑） |  | 智能体改过的文件（change_id、路径、时间、是否已撤销）。默认只看这次对话的。 |
| `rollback_file` | 会改东西（询问模式下先问，可记住） |  | 从备份还原智能体改过的文件：给 change_id 撤销一次；all_in_this_session=true 撤销这次对话的全部改动。文件之后又被改过时默认拒绝。会先问用户。 |
| `run_batch` | 会改东西（询问模式下先问，可记住） |  | 提交一次批处理（后台任务）：范围给 sample_ids / batch / q / set_id 之一，或 all_samples=true；recipe 不给就用默认配方。每个样品会多一套关键结果并成为当前值。会先问用户。提交后用 get_task 等它跑完。 |
| `get_task` | 只读（直接跑） |  | 看一个后台任务（批处理）的状态与进度；wait_seconds > 0（最多 60）时等它跑完再回。跑完了会给 run_id。 |
| `list_tasks` | 只读（直接跑） |  | 最近的后台任务：id、标题、状态、进度、成功 / 失败几个。 |
| `cancel_task` | 会改东西（询问模式下先问，可记住） |  | 停下一个正在跑的后台任务。已经跑完的样品结果保留。会先问用户。 |
| `run_command` | 会改东西（询问模式下先问，可记住）；按参数升级（如危险命令） |  | 在智能体专用目录（workspace/agent/）里跑一条命令（Windows 是 cmd，其它是 sh），返回退出码和输出。python 用的是平台自己那套（有 numpy / pandas）。写脚本用 write_file 写到 agent/ 下，再用它跑。cwd 只能是可写的文件夹；timeout 默认 60 … |
| `propose_plan` | 只读（直接跑） | 是 | （计划模式）调研完了交出执行计划：goal 一句话目标；steps 每步 {title, tool, change}（要用哪个工具、会改什么）；risks 风险；questions 要用户先回答的。调用后这一轮结束，用户批准后才执行。 |
| `batch_summary` | 只读（直接跑） |  | 一次批处理的汇总：几个样品成功 / 失败、失败的原因、每个字段这一次算出来的均值 / 中位数 / 范围。run_id 来自 get_task 或 list_runs。 |

## 5. 数据表

定义在 `app/storage/schema.sql`；改表结构要加一步迁移（`app/storage/migrations.py`）。

| 表 / 视图 | 说明（schema.sql 里紧挨着的注释） |
|---|---|
| `sample` | 实验对象 样品的身份是 (名字, 批次)。命名规则把 B20_S1 拆成 batch=B20/sample=S1， 所以 S1 这个名字在每个批次里都会出现 —— 只按名字唯一会把它们静默合并。 |
| `measurement` | 一次测量 |
| `import_batch` | 导入批次 |
| `artifact` | 文件 / 图像 / 曲线 |
| `analysis_run` | 一次处理 |
| `key_result` | 关键结果（长表） |
| `data_table` | 数值表（Parquet 索引） |
| `app_setting` | 设置 |
| `sample_set` | 样品集 存的是**筛选式**而不是 ID 列表。在上千个样品的量级上，ID 列表这个模型是坏的： 放不进 URL、一周后看不出选了什么、新导入的样品永远进不来。 |
| `task` | 后台任务 1000 个样品 × 约 1 秒 = 17 分钟，同步请求必然超时。 |
| `conversation` | AI 会话 「比完就没了、找不到在哪儿」在对比那边靠 analysis_run 解决了， 对话这边靠这两张表：问过什么、模型答过什么，刷新之后还在。 |
| `message` | 对话里的一条消息（用户 / 模型）；模型的回答连同当时给它的数据范围一起存，事后能复原「它看到了什么」。 |
| `agent_session` | 智能体（「智能体」页）的会话。和上面的 conversation 分开：那边是「带着一段事实问答」， 这边是模型自己调工具查数、改东西（改之前停下来问人）。三样东西都存在这一行里： messages_json  给模型的上下文（不含系统提示；含工具调用与工具结果，早的会被折叠） events_json    给界面回放的事件 —— 和当时推给界面的是同一种形状（不含逐字增量） pending_json   停在审批上的那一轮：等你点「批准 / 拒绝」时从这里接着跑。没有就是 NULL |
| `agent_file_change` | 智能体改过的文件（照 IFlow 的 agent_fs_changes）：改之前先把原文件拷一份到 workspace/agent_backups/<change_id>/， 这里记前后的 sha。回滚时先核对「现在的文件就是这次改出来的那份」，被别人又改过就拒绝（除非强制）。 |
| `ai_pin` | 把某条回答钉到某一次对比上。对比页据此显示「AI 分析」。 |
| `key_result_current`（视图） | 关键结果的「当前值」 key_result 是只追加的：每跑一次批处理，每个样品就再多一整套字段。 历史要留着（哪个版本、什么参数算出过什么），但「这个样品现在的膜厚是多少」 只能有一个答案 —— 不然 AI 和按字段筛选会把新旧几次的结果混在一起 （自查 §6 #2：新旧两次同时存在时，AI 拿到的是旧值）。 当前值 = 同一个 (样品, 字段) 里**最后写入、且没被标作废**的那一条。 作废了最新的，就退回上一条 —— 「这次算坏了」不等于「这个样品没有值」。 每次启动先删再建：视图定义改了也能生效，而视图本身不存数据，删了不丢东西。 |

## 6. 环境变量

| 变量 | 默认 | 在哪读 |
|---|---|---|
| `HTE_AI_DETAIL_MAX` | "40" | `app/config.py:39` |
| `HTE_ALLOWED_HOSTS` | "" | `app/security.py:45` |
| `HTE_HOST` | "127.0.0.1" | `app/config.py:41` |
| `HTE_LLM_ALLOWED_HOSTS` | "" | `app/ai/gateway.py:63` |
| `HTE_LLM_RETRIES` | "3" | `app/ai/gateway.py:35` |
| `HTE_LLM_RETRY_BASE` | "1.0" | `app/ai/gateway.py:36` |
| `HTE_LLM_TIMEOUT` | "120" | `app/ai/gateway.py:33` |
| `HTE_NO_BROWSER` | （无） | `app/main.py:215` |
| `HTE_PORT` | "8765" | `app/config.py:42` |
| `HTE_WORKSPACE` | ROOT / "workspace" | `app/config.py:19` |

## 7. QFLS 成像

阈值（`app/qfls/metrics.py` 的 `PARAMS`；每次处理把当时用的那一份存进输出目录的 run.json）：

| 参数 | 值 |
|---|---|
| `valid_range_ev` | [0.3, 3.0] |
| `invalid_out_of_range_frac` | 0.05 |
| `low_abs_ev` | 1.1 |
| `sigma_k` | 2.0 |
| `edge_band_frac` | 0.1 |
| `center_frac` | 0.5 |
| `connectivity` | 8 |
| `smooth_px` | 3 |
| `low_cc_min_px` | 5 |
| `scratch_min_px` | 30 |
| `scratch_min_elong` | 5.0 |
| `scratch_min_len_frac` | 0.05 |
| `scratch_max_width_px` | 12.0 |
| `bright_k` | 3.0 |
| `bright_min_px` | 3 |
| `bright_max_px` | 200 |
| `moran_grid` | [32, 48] |
| `moran_threshold` | 0.15 |
| `variogram_bin` | 4 |
| `variogram_max_lag_frac` | 0.33 |
| `variogram_sill_frac` | 0.95 |
| `flag_large_low_frac` | 0.01 |
| `flag_edge_ev` | 0.02 |
| `flag_group_robust_z` | 3.5 |
| `flag_group_min_n` | 4 |
| `score.spread` | {'metric': 'p90_p10', 'label': '离散（P90 − P10）', 'unit': 'eV', 'max': 25, 'from': 0.015, 'to': 0.075} |
| `score.low_frac` | {'metric': 'low_2s_frac', 'label': '低值占比（< 中位数 − 2σ）', 'unit': 'frac', 'max': 15, 'from': 0.03, 'to': 0.1} |
| `score.low_area` | {'metric': 'low_cc_max_frac', 'label': '最大低值连通域', 'unit': 'frac', 'max': 15, 'from': 0.0, 'to': 0.01} |
| `score.scratch` | {'metric': 'scratch_count', 'label': '划痕候选', 'unit': '条', 'max': 20, 'per': 10} |
| `score.edge` | {'metric': 'edge_minus_center', 'abs': True, 'label': '边缘 − 中心', 'unit': 'eV', 'max': 10, 'from': 0.005, 'to': 0.03} |
| `score.moran` | {'metric': 'morans_i', 'label': "结构不均（Moran's I）", 'unit': '', 'max': 10, 'from': 0.15, 'to': 0.5} |
| `score.bright` | {'metric': 'bright_density', 'label': '亮点密度', 'unit': '个/百万像素', 'max': 5, 'from': 0.0, 'to': 100.0} |
| `grades` | {'A': 85, 'B': 70, 'C': 55} |

指标（进分数 / 进全局比较；「跨物镜」= 不同物镜之间能不能直接比）：

| 键 | 名称 | 单位 | 跨物镜 |
|---|---|---|---|
| `mean` | 均值 | eV | **不能** |
| `median` | 中位数 | eV | **不能** |
| `std` | 标准差 | eV | 能 |
| `p90_p10` | P90 − P10 | eV | 能 |
| `p99_p1` | P99 − P1 | eV | 能 |
| `edge_minus_center` | 边缘 − 中心 | eV | 能 |
| `low_abs_frac` | 低荧光绝对占比 | — | **不能** |
| `low_2s_frac` | 低于中位数 − 2σ 占比 | — | 能 |
| `high_2s_frac` | 高于中位数 + 2σ 占比 | — | 能 |
| `low_cc_count` | 低值连通域数 | 个 | **不能** |
| `low_cc_max_frac` | 最大低值连通域占比 | — | **不能** |
| `scratch_count` | 划痕候选数 | 条 | 能 |
| `bright_density` | 亮点密度 | 个/百万像素 | **不能** |
| `morans_i` | Moran's I | — | 能 |
| `variogram_range_px` | 变异函数变程 | 像素 | **不能** |
| `score` | 综合质量分 | 分 | 能 |
