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
CREATE TABLE IF NOT EXISTS sample (
    sample_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    batch       TEXT,
    note        TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

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
