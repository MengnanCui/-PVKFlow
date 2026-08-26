# 数据模型

建表语句在 `app/storage/schema.sql`。这里说的是**为什么这么设计**。

## 一句话

原始文件按类型分层落地，结果写进一张长表，用四个 ID 把它们串起来。

---

## 四个连接键

| ID | 是什么 | 谁生成 |
|---|---|---|
| `sample_id` | 实验对象 | 导入时按文件名解析出的样品名，同名复用 |
| `measurement_id` | 一次测量 | 命名规则解析出 `method` 时创建 |
| `analysis_run_id` | 一次处理 | 每次跑 skill 生成 |
| `artifact_id` | 一个文件 / 图像 / 曲线 | 每次导入或产出派生文件时生成 |

调用方（skill、API、界面）**只认 artifact_id**，不关心文件到底在哪。
这是后期能把文件层换成 MinIO/S3 而不动上层代码的前提。

---

## key_result 为什么是长表

```sql
key_result(id, sample_id, measurement_id, analysis_run_id,
           field_name, label, value_num, value_text, unit,
           source, quality, version, artifact_uri, created_at)
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

- `source` —— `raw` / `manual` / `skill` / `ai`。模型产出的结果永远标 `ai`
- `quality` —— `validated` / `review` / `reject`。skill 默认给 `review`
- `version` —— 等于 `analysis_run.skill_version`。改了算法重跑，两批结果能区分开

没有这三个字段，一年后你会分不清某个数是怎么来的。

---

## artifact：文件的身份

```sql
artifact(artifact_id, kind, storage_mode, sha256,
         original_path, display_path, stored_path,
         filename, ext, mime, size, status, thumb_path, ...)
```

- `kind` —— `raw`（导入的）/ `derived`（skill 产出的图等）
- `storage_mode` —— `copied`（复制进 `workspace/raw/`）/ `referenced`（只登记路径）
- `sha256` —— 内容指纹。`raw` 层上有唯一索引，同样的内容只登记一次
- `status` —— `ok` / `missing`。引用型文件断链时标 `missing`，不静默失败

复制型文件按 `workspace/raw/<sha[:2]>/<sha><ext>` 存放 —— 内容寻址，
天然去重，同一份数据换个名字导入也不会存两遍。

---

## analysis_run：让结果可追溯

```sql
analysis_run(analysis_run_id, skill_id, skill_version, params_json,
             input_json, sample_id, status, source, error, log, ...)
```

**没有 analysis_run 的结果等于没有结果** —— 你不知道它是哪个算法、哪个版本、
用什么参数、从哪个文件算出来的。所有 skill 结果都必须经过
`app/skills/runner.py` 落库，因为只有那里会写这张表。

失败也记录：`status='failed'` + 完整 traceback。不会静默吞掉。

---

## data_table：Parquet 的索引

大量数值（曲线点、批量特征）不进 SQLite —— 一条 J-V 曲线上千点，
一批实验几百条曲线，塞进关系表既撑大数据库也难做列式统计。

写成 `workspace/tables/<name>__<id>.parquet`，SQLite 里只留一行索引。
第三期上 DuckDB 时可以直接 `SELECT * FROM 'tables/*.parquet'`。

pyarrow 缺失或列类型异常时会退回 CSV —— 宁可格式降级也不丢数据。

---

## 升级路线

| 现在 | 什么时候换 | 换成什么 |
|---|---|---|
| SQLite | 多人同时写 | PostgreSQL（SQL 基本不用改） |
| 本地文件系统 | 跨机器 / 需要备份策略 | MinIO / S3（只有 `artifacts.local_path()` 要改） |
| Parquet 直读 | 要做跨表关联分析 | + DuckDB 查询层 |

三条路线都不需要改数据模型本身。
