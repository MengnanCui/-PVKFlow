"""id 和时间戳 —— 全平台一份写法（C073）。

**时间**：库里、run.json、事件里的时间戳一律 **UTC**，带 `+00:00`（`now()`）；界面上用 `fmtTime` 换成本地时间显示。
唯一的例外是**测量时间** `measurement.measured_at`：那是仪器的本地时钟，按原样存（迁移第 2 步就是为了统一成本地时间）。

**id**：`前缀_12 位 hex`（`new_id("smp")`）；要能按时间排序的（QFLS 处理编号、智能体文件改动）用
`new_id("qr", sortable=True)` → `qr_20261110T060000Z_1a2b3c`（UTC 时间 + 6 位随机）。
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone


def now() -> str:
    """UTC ISO8601（秒），避免跨时区 / 夏令时的坑。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str, *, sortable: bool = False) -> str:
    if sortable:
        return f"{prefix}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
