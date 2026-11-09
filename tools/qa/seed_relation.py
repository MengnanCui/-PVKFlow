"""给**测试工作区**写两个合成字段 qa_x / qa_y，让构效关系页的验收有像样的数可看。

样例数据是同一份 Data.csv 复制出来的，所有样品的结果一模一样 —— 散点图上只有三个点，
相关系数也没法验。这里按样品顺序造一组确定的数：qa_y ≈ 2·qa_x + 噪声，
另外把一个样品的 qa_y 拉得很高（箱线图上应该是离群点）。

只对测试工作区用。它走的是平台自己的落库路径（results.start_run / write_results），
写进去的结果和真的批处理结果长得一样。

    HTE_WORKSPACE=/tmp/hte-qa .venv/bin/python tools/qa/seed_relation.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))     # 从仓库根目录找 app

from app.storage import db, results  # noqa: E402


def main() -> None:
    db.init()
    rows = db.query("SELECT sample_id, name, batch FROM sample ORDER BY name")
    for i, r in enumerate(rows):
        x = 100.0 + 3.0 * i + 5.0 * math.sin(i * 1.3)
        y = 2.0 * x + 12.0 * math.cos(i * 0.7)
        if i == len(rows) // 2:
            y += 400.0                        # 离群点
        rid = results.start_run("qa.seed_relation", "1.0.0", sample_id=r["sample_id"])
        results.write_results(rid, [
            {"field_name": "qa_x", "label": "QA 合成 X", "value": round(x, 4), "unit": "nm"},
            {"field_name": "qa_y", "label": "QA 合成 Y", "value": round(y, 4), "unit": "a.u."},
        ], sample_id=r["sample_id"])
        results.finish_run(rid, "ok")
    print(f"写了 {len(rows)} 个样品的 qa_x / qa_y")


if __name__ == "__main__":
    main()
