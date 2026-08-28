-- HTE Studio 数据模型
--
-- 设计要点：
--  * key_result 是长表（EAV）。X/Y 是「分析角色」，不是数据库列角色——
--    新增一个测量字段不需要改 schema，构效关系页后期直接对它做透视。
--  * artifact 记录文件的身份（sha256）与落地方式（复制 / 原地引用），
--    原始文件永不被修改。
--  * analysis_run 记录一次处理：用了哪个 skill、什么版本、什么参数。
--    没有它，结果就不可追溯。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 实验对象
-- 样品的身份是 (名字, 批次)。命名规则把 B20_S1 拆成 batch=B20/sample=S1，
-- 所以 S1 这个名字在每个批次里都会出现 —— 只按名字唯一会把它们静默合并。
CREATE TABLE IF NOT EXISTS sample (
    sample_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    batch       TEXT,
    note        TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sample_name_batch
    ON sample(name, COALESCE(batch, ''));
CREATE INDEX IF NOT EXISTS idx_sample_batch ON sample(batch);

-- ---------------------------------------------------------------- 一次测量
CREATE TABLE IF NOT EXISTS measurement (
    measurement_id TEXT PRIMARY KEY,
    sample_id      TEXT REFERENCES sample(sample_id) ON DELETE SET NULL,
    method         TEXT,                  -- thickness / spectrum / jv / image ...
    instrument     TEXT,
    measured_at    TEXT,
    meta_json      TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

-- ---------------------------------------------------------------- 导入批次
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id    TEXT PRIMARY KEY,
    source_hint TEXT,                     -- 用户选的目录 / 拖拽来源，便于回溯
    file_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- 文件 / 图像 / 曲线
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id   TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,          -- raw | derived
    storage_mode  TEXT NOT NULL,          -- copied | referenced
    sha256        TEXT NOT NULL,
    original_path TEXT,                   -- 导入时的绝对路径（referenced 时是唯一真身）
    display_path  TEXT,                   -- 展示用相对路径（webkitRelativePath 之类）
    stored_path   TEXT,                   -- copied 时工作区内的相对路径
    filename      TEXT NOT NULL,
    ext           TEXT NOT NULL DEFAULT '',
    mime          TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok',   -- ok | missing
    is_matrix     INTEGER,                      -- NULL=未判定 0/1=判定过（光谱矩阵）
    thumb_path    TEXT,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    sample_id     TEXT REFERENCES sample(sample_id) ON DELETE SET NULL,
    batch_id      TEXT REFERENCES import_batch(batch_id) ON DELETE SET NULL,
    produced_by   TEXT,                   -- derived 时指向 analysis_run_id
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_sha    ON artifact(sha256);
CREATE INDEX IF NOT EXISTS idx_artifact_sample ON artifact(sample_id);
CREATE INDEX IF NOT EXISTS idx_artifact_batch  ON artifact(batch_id);
CREATE INDEX IF NOT EXISTS idx_artifact_kind   ON artifact(kind);
-- 同一个文件（内容相同）在 raw 层只登记一次
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_raw_sha ON artifact(sha256) WHERE kind = 'raw';

-- ---------------------------------------------------------------- 一次处理
CREATE TABLE IF NOT EXISTS analysis_run (
    analysis_run_id TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL,
    skill_version   TEXT NOT NULL,
    skill_name      TEXT,
    params_json     TEXT NOT NULL DEFAULT '{}',
    input_json      TEXT NOT NULL DEFAULT '[]',   -- 输入的 artifact_id 列表
    sample_id       TEXT REFERENCES sample(sample_id) ON DELETE SET NULL,
    measurement_id  TEXT REFERENCES measurement(measurement_id) ON DELETE SET NULL,
    status          TEXT NOT NULL,                -- running | ok | failed
    source          TEXT NOT NULL DEFAULT 'skill',-- skill | ai | manual
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    error           TEXT,
    log             TEXT,
    parent_run_id   TEXT,                        -- 批处理：指向父运行
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_skill  ON analysis_run(skill_id);
CREATE INDEX IF NOT EXISTS idx_run_sample ON analysis_run(sample_id);
CREATE INDEX IF NOT EXISTS idx_run_status ON analysis_run(status);

-- ---------------------------------------------------------------- 关键结果（长表）
CREATE TABLE IF NOT EXISTS key_result (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT REFERENCES sample(sample_id) ON DELETE SET NULL,
    measurement_id  TEXT REFERENCES measurement(measurement_id) ON DELETE SET NULL,
    analysis_run_id TEXT REFERENCES analysis_run(analysis_run_id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,        -- PCE / thickness / additive ...
    label           TEXT,
    value_num       REAL,                 -- 数值走这里，便于统计与筛选
    value_text      TEXT,                 -- 文本 / 分类走这里
    unit            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL,        -- raw | manual | skill | ai
    quality         TEXT NOT NULL DEFAULT 'review',  -- validated | review | reject
    version         TEXT,                 -- 算法版本，等于 analysis_run.skill_version
    artifact_uri    TEXT,                 -- 关联到具体文件
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kr_field   ON key_result(field_name);
CREATE INDEX IF NOT EXISTS idx_kr_sample  ON key_result(sample_id);
CREATE INDEX IF NOT EXISTS idx_kr_run     ON key_result(analysis_run_id);
CREATE INDEX IF NOT EXISTS idx_kr_quality ON key_result(quality);

-- ---------------------------------------------------------------- 数值表（Parquet 索引）
CREATE TABLE IF NOT EXISTS data_table (
    table_id        TEXT PRIMARY KEY,
    analysis_run_id TEXT REFERENCES analysis_run(analysis_run_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,        -- 例如 jv_curve / spectrum
    path            TEXT NOT NULL,        -- 工作区内相对路径，指向 .parquet
    n_rows          INTEGER NOT NULL DEFAULT 0,
    columns_json    TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_table_run ON data_table(analysis_run_id);

-- ---------------------------------------------------------------- 设置
CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ================================================================ 批处理
-- 一次批处理 = 一条父运行 + 每个样品一条子运行。
-- 为什么每个样品单独记一条：上千个样品里一定有跑失败的，你必须知道是哪些、
-- 为什么。一条大记录说不清楚。
-- parent_run_id 由 db.py 的 _add_missing_columns() 补上 ——
-- ALTER TABLE 不是幂等的，不能放在每次启动都执行的脚本里。
CREATE INDEX IF NOT EXISTS idx_run_parent ON analysis_run(parent_run_id);

-- ---------------------------------------------------------------- 样品集
-- 存的是**筛选式**而不是 ID 列表。在上千个样品的量级上，ID 列表这个模型是坏的：
-- 放不进 URL、一周后看不出选了什么、新导入的样品永远进不来。
CREATE TABLE IF NOT EXISTS sample_set (
    set_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'dynamic',  -- dynamic | pinned
    filter_json     TEXT NOT NULL DEFAULT '{}',       -- dynamic：随新数据生长
    pinned_ids_json TEXT NOT NULL DEFAULT '[]',       -- pinned：钉死的快照
    note            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sample_set_name ON sample_set(name);

-- ---------------------------------------------------------------- 后台任务
-- 1000 个样品 × 约 1 秒 = 17 分钟，同步请求必然超时。
CREATE TABLE IF NOT EXISTS task (
    task_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- batch.abs_thickness | ...
    title       TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,              -- queued | running | ok | failed | cancelled
    progress    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    n_ok        INTEGER NOT NULL DEFAULT 0,
    n_failed    INTEGER NOT NULL DEFAULT 0,
    message     TEXT,                       -- 当前在做什么，给进度条配文字
    error       TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
CREATE INDEX IF NOT EXISTS idx_task_created ON task(created_at DESC);

-- 关键字段的数值筛选（"PCE 大于 20 的样品"）。
-- 相关子查询是按 sample_id 关联的，所以 sample_id 必须在索引最前面 ——
-- 只建 (field_name, value_num) 的话关联那一步用不上索引。
CREATE INDEX IF NOT EXISTS idx_kr_field_value ON key_result(field_name, value_num);
CREATE INDEX IF NOT EXISTS idx_kr_sample_field ON key_result(sample_id, field_name, value_num);

-- 分面与筛选用到的相关子查询
-- 按时间筛选走这条
CREATE INDEX IF NOT EXISTS idx_mea_measured_at
    ON measurement(measured_at);
CREATE INDEX IF NOT EXISTS idx_mea_sample_method ON measurement(sample_id, method);
CREATE INDEX IF NOT EXISTS idx_artifact_matrix ON artifact(sample_id, is_matrix);
CREATE INDEX IF NOT EXISTS idx_artifact_path ON artifact(display_path);
-- 导入去重要按绝对路径查一次（「同一个文件再导一遍」）
CREATE INDEX IF NOT EXISTS idx_artifact_origpath ON artifact(original_path);

-- ---------------------------------------------------------------- AI 会话
-- 「比完就没了、找不到在哪儿」在对比那边靠 analysis_run 解决了，
-- 对话这边靠这两张表：问过什么、模型答过什么，刷新之后还在。

CREATE TABLE IF NOT EXISTS conversation (
    conversation_id TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '新对话',
    -- 这次对话的数据范围。存的是**筛选式**，不是样品 ID 列表 ——
    -- 跟 sample_set 一个道理：规则可以复算，快照会过期。
    scope_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    message_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    role            TEXT NOT NULL,              -- user | assistant | system
    content         TEXT NOT NULL DEFAULT '',
    -- 结构化动作卡片、用量、被中断的标记都塞这里，正文保持是纯文本
    meta_json       TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

-- 把某条回答钉到某一次对比上。对比页据此显示「AI 分析」。
CREATE TABLE IF NOT EXISTS ai_pin (
    pin_id          TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL,
    conversation_id TEXT,
    message_id      TEXT,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON message(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversation(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pin_run ON ai_pin(analysis_run_id, created_at DESC);
