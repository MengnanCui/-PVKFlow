"""QFLS 处理的后台任务执行体（和批处理的 app/batch.run_batch 一样放在领域层，不放路由里）。

注册发生在 import 时：app/api/qfls.py import 了它，服务启动时就注册上了。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app import tasks
from app.qfls import demo, pipeline

KIND = "qfls_process"


@tasks.register(KIND)
def run_qfls(ctx: tasks.TaskContext) -> dict:
    """一次 QFLS 处理（演示数据时先生成）。参数：{root | demo, full, run_id, title, figures, params}"""
    p = ctx.params
    root = p.get("root")
    if p.get("demo"):
        root = str(pipeline.demo_dir())
        if Path(root).exists():          # 演示数据是平台自己生成的，重新生成前清掉旧的
            shutil.rmtree(root)
        demo.generate(root, full=bool(p.get("full")),
                      progress=lambda i, n: ctx.progress(i, n, f"生成演示数据 {i} / {n}"))
    run = pipeline.process(root, run_id=p["run_id"], figures=p.get("figures", True), title=p.get("title", ""),
                           params=p.get("params") or None, progress=ctx.progress)
    return {"run_id": run["run_id"], "n_frames": run.get("n_frames"), "n_valid": run.get("n_valid"),
            "n_anomaly": run.get("n_anomaly")}
