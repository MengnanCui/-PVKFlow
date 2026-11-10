"""数值表落地：大量曲线点、批量特征走 Parquet。

为什么不塞进 SQLite：一条 J-V 曲线上千点，一批实验几百条曲线，
放进关系表既撑大数据库也难做列式统计。Parquet 是列存、压缩好，
第三期上 DuckDB 时可以直接 `SELECT * FROM 'tables/*.parquet'` 查。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import config
from app.storage import db
from app.errors import NotFound


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60] or "table"


def write_table(analysis_run_id: str, name: str, frame: Any) -> dict:
    """把一个 DataFrame（或 dict-of-lists）写成 Parquet 并登记。"""
    import pandas as pd

    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    config.ensure_dirs()

    table_id = db.new_id("tbl")
    rel = Path("tables") / f"{_safe(name)}__{table_id}.parquet"
    target = config.WORKSPACE / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_parquet(target, index=False)
    except Exception:
        # pyarrow 缺失或列类型异常时退回 CSV，宁可格式降级也不丢数据
        rel = rel.with_suffix(".csv")
        target = config.WORKSPACE / rel
        df.to_csv(target, index=False)

    with db.tx() as c:
        c.execute(
            "INSERT INTO data_table(table_id, analysis_run_id, name, path, n_rows,"
            " columns_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (table_id, analysis_run_id, name, rel.as_posix(), int(len(df)),
             json.dumps([str(x) for x in df.columns], ensure_ascii=False), db.now()),
        )
    return {"table_id": table_id, "name": name, "path": rel.as_posix(),
            "n_rows": int(len(df)), "columns": [str(x) for x in df.columns]}


# 长表里这几列是**文本**：样品 ID、样品名、批次。读 CSV 时必须按文本读。
#
# pandas 默认会猜类型：样品名 "001" 变成 1、"20260729" 变成 20260729.0，
# 空批次和 "NA" 变成 NaN —— 于是图例上出现 `nan/S1`、导出的 data.csv 里
# 出现 `20260729.0/…`（自查 §6 #10）。pyarrow 没装时 CSV 是默认格式，
# 这条路径是主路径，不是兜底。
TEXT_COLUMNS = ("sample_id", "sample_name", "batch", "label", "group")


def read_frame(path: Path):
    """读一张长表（CSV 或 Parquet）。CSV 里的文本列按原样读回。"""
    import pandas as pd

    if path.suffix != ".csv":
        return pd.read_parquet(path)
    cols = list(pd.read_csv(path, nrows=0).columns)
    text = [c for c in cols if c in TEXT_COLUMNS]
    # 文本列：空就是空串、"NA" 就是 "NA"；数值列：空才是缺失
    return pd.read_csv(path, dtype={c: str for c in text}, keep_default_na=False,
                       na_values={c: ["", "nan", "NaN"] for c in cols if c not in text})


def read_table(table_id: str, limit: int | None = None) -> dict:
    row = db.query_one("SELECT * FROM data_table WHERE table_id = ?", (table_id,))
    if not row:
        raise NotFound(f"数值表不存在：{table_id}")


    path = config.WORKSPACE / row["path"]
    if not path.exists():
        raise FileNotFoundError(f"数值表文件缺失：{row['path']}")
    df = read_frame(path)
    if limit:
        df = df.head(limit)
    return {
        "table_id": table_id,
        "name": row["name"],
        "columns": [str(c) for c in df.columns],
        "n_rows": int(row["n_rows"]),
        "rows": df.where(df.notna(), None).values.tolist(),
    }


def tables_for_run(analysis_run_id: str) -> list[dict]:
    return db.query(
        "SELECT table_id, name, path, n_rows, columns_json FROM data_table"
        " WHERE analysis_run_id = ? ORDER BY rowid", (analysis_run_id,),
    )
