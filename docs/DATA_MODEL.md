# 数据模型

建表语句在 `app/storage/schema.sql`（15 张表 + 1 个视图），版本与迁移在 `app/storage/migrations.py`
（它另建一张 `schema_migration` 记每一步升级）。这里说的是**为什么这么设计**。

## 一句话

原始文件按类型分层落地，结果写进一张长表，用四个 ID 把它们串起来。

---

## 四个连接键

| ID | 是什么 | 谁生成 |
|---|---|---|
| `sample_id` | 实验对象 | 导入时解析出的（样品名, 批次），同名同批次才复用。仪器数据一个子文件夹 = 一次测量 = 一个样品 |
| `measurement_id` | 一次测量 | 命名规则解析出 `method` 时创建；`measured_at` 见下 |
| `analysis_run_id` | 一次处理 | 每次批处理生成一条（父），每个样品再一条（子，`parent_run_id` 指向父） |
| `artifact_id` | 一个文件 / 图像 / 曲线 | 每次导入或产出派生文件时生成 |

调用方（模块、API、界面）**只认 artifact_id**，不关心文件到底在哪。
这是后期能把文件层换成 MinIO/S3 而不动上层代码的前提。

---

## key_result 为什么是长表

```sql
key_result(id, sample_id, measurement_id, analysis_run_id,
           field_name, label, value_num, value_text, unit,
           source, quality, quality_note, quality_at, version, artifact_uri, created_at)
```

一行一个字段值，而不是「一行一个样品、每个测量量占一列」。

**好处**：新增一个测量量（比如某天开始测迟滞指数）不需要改 schema、不需要
迁移、不需要改任何代码。构效关系页直接按 `field_name` 透视就能把任意字段
当 X 或 Y —— 这正是「X/Y 是分析角色，不是数据库列角色」的落地方式。

**代价**：要拿「某个样品的所有字段」得做透视。在这个体量下（单机、十万量级）
SQLite 完全扛得住；真到扛不住的那天，`field_name` 上有索引，
上 PostgreSQL 或物化一张宽表都是增量改动。

数值进 `value_num`，文本 / 分类进 `value_text`，两者互斥。
统计和筛选只看 `value_num`，不用到处 CAST。

### 每条结果自带的元信息

- `source` —— `raw` / `manual` / `skill` / `ai`。模型产出的结果永远标 `ai`；
  `skill` 是历史名字，现在的意思是「平台或功能模块算出来的」（列名不改：改名要做迁移，换不来任何东西）
- `quality` —— `validated`（可信）/ `review`（待查）/ `reject`（作废）。列的默认值是 `review`；
  平台的批处理写入时给 `validated`（它自己的检查过了）。作废的不进 `key_result_current`，当前值退回上一次没作废的。
  在样品页「关键结果」和构效关系页上审核
- `quality_note` / `quality_at` —— 最近一次**人**审核的理由和时间（迁移第 4 步加的）。`quality_at` 为空 = 状态是写结果的程序给的，还没人审过
- `version` —— 等于 `analysis_run.skill_version`，也就是算出它的那个模块的版本。改了算法重跑，两批结果能区分开

没有这几个字段，一年后你会分不清某个数是怎么来的。

### 当前值与历史：`key_result_current`

`key_result` **只追加**：每跑一次批处理，每个样品就再多一整套字段。历史要留着，
但「这个样品现在的 OT 末值是多少」只能有一个答案。所以有一个视图：

> **当前值** = 同一个 (样品, 字段) 里**最后写入、且没被标作废**的那一条。
> 作废了最新的就退回上一条 —— 「这次算坏了」不等于「这个样品没有值」。

| 读的是 | 谁 |
|---|---|
| `key_result_current`（当前值） | AI 上下文的逐样品标量、按字段筛选与分面、字段列表（构效关系页）、每个样品的结果数、总览计数、`/api/results` 默认 |
| `key_result`（全部历史） | 某一次运行自己的结果（对比页、运行详情）、`/api/results?history=true` |

原来所有读取都直接读整张表，新旧几次混在一起 —— AI 曾拿着旧值回答（自查 §6 #2）。

字段列表（`results.list_fields`）**一个字段一行**：同一个字段在不同样品上写了不同单位时，所有单位都列出来并标 `mixed_units`
（这时的最小 / 最大是把不同单位的数混在一起算的）。原来按（字段, 单位）分组，换了单位就列两行（自查 §6 #28）。

---

## measurement：测量时间只有一种写法

`measured_at` 是「按时间筛选」的唯一来源，**存本地时间、不带时区**（`2026-07-29T18:35:47`）：
- 首选文件夹名里的时间戳（仪器写的本地时间）
- 文件夹名里没有时就退回文件修改时间，也换成本地时间

按时间筛选是字符串比较（ISO 8601 是字典序可比的，索引也用得上），所以两种来源必须是同一种写法。
原来退回文件修改时间的那一路存的是 UTC（带 `+00:00`），在中国就偏 8 小时；第 2 步迁移把它们换算过来了。

---

## artifact：文件的身份

```sql
artifact(artifact_id, kind, storage_mode, sha256,
         original_path, display_path, stored_path,
         filename, ext, mime, size, status, thumb_path, ...)
```

- `kind` —— `raw`（导入的）/ `derived`（处理产出的图等）
- `storage_mode` —— `copied`（复制进 `workspace/raw/`）/ `referenced`（只登记路径）
- `sha256` —— 内容指纹。`raw` 层上有唯一索引，同样的内容只登记一次
- `status` —— `ok` / `missing`。引用型文件断链时标 `missing`，不静默失败
- `is_matrix` —— 是不是光谱矩阵。**导入时就判**（`matrix.probe()`，只读文件头），筛选式的「有光谱矩阵」和批处理靠它；
  导入时判不出来就留空，列样品时再用同一个函数补

复制型文件按 `workspace/raw/<sha[:2]>/<sha><ext>` 存放 —— 内容寻址，
天然去重，同一份数据换个名字导入也不会存两遍。

---

## analysis_run：让结果可追溯

```sql
analysis_run(analysis_run_id, parent_run_id, skill_id, skill_version, params_json,
             input_json, sample_id, status, source, error, log, ...)
```

**没有 analysis_run 的结果等于没有结果** —— 你不知道它是哪个算法、哪个版本、
用什么参数、从哪个文件算出来的。所有结果都经过 `results.start_run → write_results → finish_run`
落库（批处理在 `app/batch.py` 里这样做）。`skill_id` / `skill_version` 是历史列名，存的是模块的 id 和版本。

失败也记录：`status='failed'` + 完整 traceback。不会静默吞掉。

---

## data_table：Parquet 的索引

大量数值（曲线点、批量特征）不进 SQLite —— 一条 J-V 曲线上千点，
一批实验几百条曲线，塞进关系表既撑大数据库也难做列式统计。

写成 `workspace/tables/<name>__<id>.csv`（装了 pyarrow 则是 `.parquet`），SQLite 里只留一行索引。
CSV 读回时样品 ID / 样品名 / 批次按文本读（`tabular.read_frame`），不然 `001` 会变成 1、空批次会变成 NaN。
第三期上 DuckDB 时可以直接 `SELECT * FROM 'tables/*.parquet'`。

pyarrow 缺失或列类型异常时会退回 CSV —— 宁可格式降级也不丢数据。

---

## 其余几张表

| 表 | 存什么 |
|---|---|
| `import_batch` | 一次导入（从哪个目录、什么时候），便于回溯 |
| `sample_set` | 保存的样品集：动态的存筛选式（随新数据生长），钉死的存一份 ID 快照 |
| `task` | 后台任务（批处理）：进度、取消、出错原因；服务重启时正在跑的标成 `interrupted`，不会永远挂在「进行中」 |
| `conversation` / `message` | AI 对话。抽屉第一次发送时才建会话；没有消息的会话不进历史列表。被打断的回答也存（`meta.aborted`），已经说出来的字不丢 |
| `ai_pin` | 钉在某次对比上的 AI 分析 |
| `agent_file_change` | 智能体改过的文件：路径、改前改后的 sha、备份放在哪（`workspace/agent_backups/<改动 id>/`）、撤销过没有。撤销时先核对当前文件就是这次改出来的那份 |
| `agent_session` | 智能体的会话，一行装三样：给模型的上下文（含工具调用与结果，早的会折叠）、给界面回放的事件（不含逐字增量）、停在审批上的那一轮（`pending_json`，批了 / 拒了就清掉）。也是第一句话发出时才建 |
| `app_setting` | 平台设置（模型密钥不在这里，在 `workspace/config/providers.json`） |
| `schema_migration` | 数据库升级到了第几步、每一步什么时候做的 |

---

## 升级路线

| 现在 | 什么时候换 | 换成什么 |
|---|---|---|
| SQLite | 多人同时写 | PostgreSQL（SQL 基本不用改） |
| 本地文件系统 | 跨机器 / 需要备份策略 | MinIO / S3（只有 `artifacts.local_path()` 要改） |
| Parquet 直读 | 要做跨表关联分析 | + DuckDB 查询层 |

三条路线都不需要改数据模型本身。
