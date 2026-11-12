"""从代码生成 docs/REFERENCE.md —— 「查表用」的那一半文档，**不许手改**。

    .venv/bin/python tools/gen_reference.py           # 重新生成
    .venv/bin/python tools/gen_reference.py --check   # 只检查是不是最新的（tests/test_reference_doc.py 也在查）

生成的内容：菜单与页面、全部接口、后台任务种类、智能体工具、数据表、环境变量、QFLS 阈值与指标。
这些东西改了代码就会变，手写的文档迟早和代码对不上；生成的永远对得上。
讲「为什么、怎么想的」的那一半在 docs/HANDOFF.md，那份是人写的。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "REFERENCE.md"
sys.path.insert(0, str(ROOT))


def first_sentence(text: str) -> str:
    t = " ".join((text or "").strip().split("\n\n")[0].split())
    return (t[:160] + "…") if len(t) > 160 else t


def cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def pages() -> list[str]:
    app_js = (ROOT / "web/js/app.js").read_text(encoding="utf-8")
    groups = {g: re.findall(r"'([\w-]+)'", re.search(rf"const {g} = \[([^\]]*)\]", app_js).group(1))
              for g in ("MAIN_NAV", "ANALYSIS_NAV", "SYSTEM_NAV")}
    where = {pid: name for name, g in (("平台", "MAIN_NAV"), ("分析", "ANALYSIS_NAV"), ("系统", "SYSTEM_NAV"))
             for pid in groups[g]}
    rows = ["| 侧栏分组 | 页面 id（地址栏 `#id/参数`） | 标题 | 说明 | 文件 |", "|---|---|---|---|---|"]
    for f in sorted((ROOT / "web/js/pages").glob("*.js")):
        src = f.read_text(encoding="utf-8")
        m = re.search(r"export const meta = \{(.*?)\n\};", src, re.S)
        if not m:
            continue
        body = m.group(1)

        def get(k: str, body: str = body) -> str:
            # 长的写成 '…' + '…'（前端一行不超过 140 个字符），拼回来
            hit = re.search(rf"{k}:\s*((?:'[^']*'\s*\+?\s*)+)", body)
            return "".join(re.findall(r"'([^']*)'", hit.group(1))) if hit else ""
        pid, parent = get("id"), get("parent")
        group = where.get(pid) or (f"下钻页（从「{parent}」进入）" if parent else "不在侧栏")
        rows.append(f"| {group} | `{pid}` | {cell(get('title'))} | {cell(get('desc'))} | `web/js/pages/{f.name}` |")
    order = {"平": 0, "分": 1, "系": 2, "下": 3, "不": 4}
    head, body = rows[:2], rows[2:]
    return head + sorted(body, key=lambda r: order.get(r[2], 9))


def _body_name(op: dict) -> str:
    """请求体是哪个模型（app/api/bodies.py）；上传文件的是 multipart，设置是任意键。"""
    rb = op.get("requestBody")
    if not rb:
        return ""
    for media, c in rb.get("content", {}).items():
        sch = c.get("schema", {})
        refs = [sch.get("$ref")] + [x.get("$ref") for x in sch.get("anyOf", [])]
        name = next((r.rsplit("/", 1)[1] for r in refs if r), "")
        if media.startswith("multipart"):
            return "文件（multipart）"
        if name:
            return f"[`{name}`](#{name.lower()})"
    return "任意键（见说明）"


def _type(sch: dict) -> str:
    if "anyOf" in sch:
        return " \\| ".join(_type(x) for x in sch["anyOf"])
    t = sch.get("type")
    if t == "array":
        return f"list[{_type(sch.get('items', {}))}]" if sch.get("items") else "list"
    if t == "object":
        return "object"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "null"}.get(t, "any")


def bodies(app) -> list[str]:
    """每个请求体模型的字段。**只收这里列的键**，别的键回 400（app/api/bodies.py）。"""
    spec = app.openapi()
    used = set()
    for ops in spec["paths"].values():
        for op in ops.values():
            for c in (op.get("requestBody") or {}).get("content", {}).values():
                sch = c.get("schema", {})
                for r in [sch.get("$ref")] + [x.get("$ref") for x in sch.get("anyOf", [])]:
                    if r:
                        used.add(r.rsplit("/", 1)[1])
    comps = spec.get("components", {}).get("schemas", {})
    out = ["定义在 `app/api/bodies.py`。**只收列出来的键**，拼错的键回 400「不认识的参数」；默认值 = 不给时的行为。", ""]
    for name in sorted(n for n in used if n in comps and not n.startswith("Body_")):
        sch = comps[name]
        out += [f"#### {name}", "", "| 字段 | 类型 | 默认 | 说明 |", "|---|---|---|---|"]
        req = set(sch.get("required", []))
        for f, fs in sch.get("properties", {}).items():
            dflt = "必填" if f in req else ("" if "default" not in fs else f"`{json.dumps(fs['default'], ensure_ascii=False)}`")
            out.append(f"| `{f}` | {_type(fs)} | {dflt} | {cell(fs.get('description', ''))} |")
        out.append("")
    return out


def routes(app) -> list[str]:
    spec = app.openapi()
    src_of = {}                       # tag → 定义它的文件（看 APIRouter(..., tags=[...])）
    for f in sorted((ROOT / "app/api").glob("*.py")):
        for t in re.findall(r'tags=\["(\w+)"\]', f.read_text(encoding="utf-8")):
            src_of[t] = f"app/api/{f.name}"
    by_tag: dict[str, list] = {}
    for path, ops in spec["paths"].items():
        if not path.startswith("/api/") or path in ("/api/docs", "/api/openapi.json"):
            continue
        for method, op in ops.items():
            tag = (op.get("tags") or ["其它"])[0]
            params = ", ".join(p["name"] for p in op.get("parameters", []) if p.get("in") == "query")
            src = src_of.get(tag, "app/main.py")
            by_tag.setdefault(tag, []).append(
                (path, method.upper(), params, _body_name(op), first_sentence(op.get("description", "")), src))
    out = []
    for tag in sorted(by_tag):
        rows = sorted(by_tag[tag])
        out += [f"### {tag}（`{rows[0][5]}`）", "", "| 方法 | 路径 | 查询参数 | 请求体 | 说明 |", "|---|---|---|---|---|"]
        out += [f"| {m} | `{p}` | {cell(q)} | {b} | {cell(d)} |" for p, m, q, b, d, _ in rows]
        out.append("")
    n = sum(len(v) for v in by_tag.values())
    return [f"共 {n} 个接口。全部是本机接口（只认 127.0.0.1 / localhost，见 `app/security.py`）；"
            "错误一律是 `{\"error\": {\"message\", \"kind\", \"detail\"}}`。交互式文档：启动后打开 `/api/docs`。", ""] + out


def task_kinds() -> list[str]:
    from app import tasks
    rows = ["| 任务种类 | 执行函数 | 说明 |", "|---|---|---|"]
    for kind, fn in sorted(tasks._HANDLERS.items()):
        rows.append(f"| `{kind}` | `{fn.__module__}.{fn.__name__}` | {cell(first_sentence(fn.__doc__ or ''))} |")
    return rows


def agent_tools() -> list[str]:
    from app.agent import tools
    risk = {"read": "只读（直接跑）", "write": "会改东西（询问模式下先问，可记住）", "irreversible": "不可逆（每次都问）"}
    rows = ["| 工具 | 风险 | 只在计划模式 | 说明 |", "|---|---|---|---|"]
    for t in tools.TOOLS:
        r = risk[t.risk] + ("；按参数升级（如危险命令）" if t.risk_fn else "")
        rows.append(f"| `{t.name}` | {r} | {'是' if t.plan_only else ''} | {cell(first_sentence(t.description))} |")
    return [f"共 {len(tools.TOOLS)} 个，定义在 `app/agent/tools.py` 的 `TOOLS`。", ""] + rows


def tables() -> list[str]:
    sql = (ROOT / "app/storage/schema.sql").read_text(encoding="utf-8")
    rows = ["| 表 / 视图 | 说明（schema.sql 里紧挨着的注释） |", "|---|---|"]
    lines = sql.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"CREATE (TABLE IF NOT EXISTS|VIEW) (\w+)", ln)
        if not m:
            continue
        com, j = [], i - 1
        while j >= 0 and (not lines[j].strip() or lines[j].startswith("DROP ")) and i - j < 3:
            j -= 1                    # 注释和 CREATE 之间隔着一个空行 / DROP VIEW 也算紧挨着
        while j >= 0 and lines[j].startswith("--"):
            t = lines[j].lstrip("- ").strip()
            if t and not set(t) <= {"-"}:
                com.insert(0, t)
            j -= 1
        rows.append(f"| `{m.group(2)}`{'（视图）' if m.group(1) == 'VIEW' else ''} | {cell(' '.join(com))} |")
    return rows


def env_vars() -> list[str]:
    rows = ["| 变量 | 默认 | 在哪读 |", "|---|---|---|"]
    seen = {}
    for f in sorted((ROOT / "app").rglob("*.py")):
        for n, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r'environ\.get\("(HTE_\w+)"(?:,\s*([^)]+?))?\)', ln):
                seen.setdefault(m.group(1), (m.group(2) or "（无）", f"{f.relative_to(ROOT).as_posix()}:{n}"))
    rows += [f"| `{k}` | {cell(v[0].strip())} | `{v[1]}` |" for k, v in sorted(seen.items())]
    return rows


def qfls() -> list[str]:
    from app.qfls.metrics import METRICS, PARAMS
    out = ["阈值（`app/qfls/metrics.py` 的 `PARAMS`；每次处理把当时用的那一份存进输出目录的 run.json）：", "",
           "| 参数 | 值 |", "|---|---|"]
    for k, v in PARAMS.items():
        if isinstance(v, dict) and k == "score":
            for kk, vv in v.items():
                out.append(f"| `score.{kk}` | {cell(vv)} |")
        else:
            out.append(f"| `{k}` | {cell(v)} |")
    out += ["", "指标（进分数 / 进全局比较；「跨物镜」= 不同物镜之间能不能直接比）：", "",
            "| 键 | 名称 | 单位 | 跨物镜 |", "|---|---|---|---|"]
    out += [f"| `{k}` | {lab} | {u or '—'} | {'能' if x else '**不能**'} |" for k, lab, u, x in METRICS]
    return out


def build() -> str:
    os.environ.setdefault("HTE_WORKSPACE", tempfile.mkdtemp(prefix="hte_ref_"))
    from app.main import app
    parts = [
        "# 参考手册（自动生成，不要手改）",
        "",
        "> 由 `tools/gen_reference.py` 从代码生成；`tests/test_reference_doc.py` 检查它是不是最新的 —— 改了接口、页面、工具、表、",
        "> 环境变量或 QFLS 阈值之后跑一下 `.venv/bin/python tools/gen_reference.py`。讲设计思路的是 `docs/HANDOFF.md`。",
        "",
        "## 1. 菜单与页面", "", *pages(), "",
        "## 2. 接口", "", *routes(app),
        "### 请求体", "", *bodies(app),
        "## 3. 后台任务种类", "", "一次只跑一个（`app/tasks.py`），状态存在 `task` 表里，前端轮询 `/api/tasks/{id}`。", "", *task_kinds(), "",
        "## 4. 智能体工具", "", *agent_tools(), "",
        "## 5. 数据表", "", "定义在 `app/storage/schema.sql`；改表结构要加一步迁移（`app/storage/migrations.py`）。", "", *tables(), "",
        "## 6. 环境变量", "", *env_vars(), "",
        "## 7. QFLS 成像", "", *qfls(), "",
    ]
    return "\n".join(parts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = build()
    if a.check:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("docs/REFERENCE.md 是最新的" if ok else "docs/REFERENCE.md 过期了：跑 .venv/bin/python tools/gen_reference.py")
        sys.exit(0 if ok else 1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"写好了 {OUT.relative_to(ROOT)}（{len(text.splitlines())} 行）")
