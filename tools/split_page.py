"""把一个过大的页面文件按职责拆成几个文件（一次性的重构工具，留着备查）。

用法：python tools/split_page.py <页面.js> <拆分方案.json>

拆分方案：{"entry": "batch.js",
           "files": {"batch-state.js": ["S", "refs", ...], "batch-curves.js": [...], ...}}
没写进任何文件的顶层定义留在入口文件里。

做的事：
1. 把文件切成「顶层定义 + 它前面紧挨着的注释」一块一块
2. 按方案分到各文件，保持原来的先后顺序
3. 每个文件只 import 它真用到的名字（从原文件的 import 里挑）
4. 跨文件用到的定义自动加 export，并在使用方补上 import
5. `let refs = {}` 这种会被整体重新赋值的模块级变量：ES 模块的导入方不能给它赋值，
   所以改成 `export const refs = {}` + `resetRefs(next)`

前端零构建，所以拆出来的文件就是浏览器直接加载的文件；离线烤制工具要求的
「只有两种 import、没有 default export、依赖无环」由 tests/test_frontend_bundle.py 守着。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEF = re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+([\w$]+)|const\s+([\w$]+)\s*=|let\s+([\w$]+)\s*=)",
                 re.M)
IMPORT = re.compile(r"^import\s+(?:\{([\s\S]*?)\}|\*\s+as\s+(\w+))\s+from\s+'([^']+)';?[ \t]*\n", re.M)


def strip_comments(src: str) -> str:
    """判断「用没用到」之前先去掉注释：注释里提一句某个函数，不该因此多一条 import（甚至成环）。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(^|[^:'\"])//.*$", r"\1", src, flags=re.M)


def uses(body: str, name: str) -> bool:
    body = strip_comments(body)
    return re.search(rf"(?<![\w$])(?<![^.]\.){re.escape(name)}(?![\w$])", body) is not None


def split(path: Path, plan: dict) -> dict[str, str]:
    src = path.read_text(encoding="utf-8")
    imports = list(IMPORT.finditer(src))
    head_end = imports[-1].end()
    file_doc = src[:imports[0].start()]
    body = src[head_end:]

    # 原文件的 import：名字 → (来源, 原写法)
    imported: dict[str, tuple[str, str]] = {}
    for m in imports:
        if m.group(2):
            imported[m.group(2)] = (m.group(3), f"* as {m.group(2)}")
            continue
        for item in m.group(1).split(","):
            item = item.strip()
            if item:
                local = item.split(" as ")[-1].strip()
                imported[local] = (m.group(3), item)

    # 顶层定义切块：从定义开始往回吞掉紧挨着的注释和空行
    lines = body.split("\n")
    starts = []
    for m in DEF.finditer(body):
        line_no = body[:m.start()].count("\n")
        starts.append((line_no, m.group(1) or m.group(2) or m.group(3)))
    chunks = []          # (name, text)
    prev_start = prev_end = 0
    lead = None
    for idx, (ln, name) in enumerate(starts):
        s = ln
        while s - 1 >= prev_end and (lines[s - 1].strip() == "" or lines[s - 1].lstrip().startswith(("//", "/*", "*"))):
            s -= 1
        if idx == 0:
            lead = "\n".join(lines[:s])
        else:
            chunks[-1] = (chunks[-1][0], "\n".join(lines[prev_start:s]))
        prev_start, prev_end = s, ln + 1
        chunks.append((name, None))
    chunks[-1] = (chunks[-1][0], "\n".join(lines[prev_start:]))
    if lead and lead.strip():
        raise SystemExit(f"第一个定义之前还有代码：{lead[:80]}")

    owner = {}
    for fname, names in plan["files"].items():
        for n in names:
            owner[n] = fname
    entry = plan["entry"]
    for name, _ in chunks:
        owner.setdefault(name, entry)
    unknown = set(owner) - {n for n, _ in chunks} - {"resetRefs"}
    if unknown:
        raise SystemExit(f"方案里有不存在的定义：{unknown}")

    files: dict[str, list[tuple[str, str]]] = {}
    for name, text in chunks:
        files.setdefault(owner[name], []).append((name, text))

    # 拆出去的文件要回头调入口里的函数（重新查询、重画列表…）：经 hooks 对象转一道。
    # 直接 import 入口会成环，而离线烤制要求依赖无环。入口加载时把这几个函数登记进去。
    hooks = plan.get("hooks", [])
    if hooks:
        home = plan["hooks_in"]
        texts = dict(chunks)
        for n in hooks:
            if owner.get(n) != entry or not re.search(rf"^(?:async\s+)?function\s+{n}\b", texts[n], re.M):
                raise SystemExit(f"hook {n} 必须是入口文件里用 function 声明的（登记时要已经提升）")
        used = set()
        for f in files:
            if f == entry:
                continue
            fixed = []
            for name, text in files[f]:
                for n in hooks:
                    text, k = re.subn(rf"(?<![\w$.]){re.escape(n)}\(", f"hooks.{n}(", text)
                    used |= {n} if k else set()
                    if uses(text.replace(f"hooks.{n}", ""), n):
                        raise SystemExit(f"{f} 里不是以调用的形式用到了 {n}，hooks 转不了")
                fixed.append((name, text))
            files[f] = fixed
        files[home].append(("hooks",
            "\n/**\n * 拆出去的文件要回头调页面入口里的几个函数（"
            + "、".join(sorted(used)) + "）。\n"
            " * 直接 import 入口会成环（离线烤制要求依赖无环），所以由入口在加载时登记在这里。\n */\n"
            "export const hooks = {};"))
        owner["hooks"] = home
        files[entry].insert(0, ("__register_hooks",
            "// 拆出去的文件经 hooks 回调这几个（见 " + home + "）\n"
            f"Object.assign(hooks, {{ {', '.join(sorted(used))} }});\n"))
    defined = {f: {n for n, _ in items} for f, items in files.items()}

    # refs 这类会被整体重新赋值的
    reassigned = {n for n, t in chunks if t.lstrip("\n").split("\n")[-1:] and
                  re.search(rf"^let\s+{n}\s*=", t, re.M) and
                  re.search(rf"(?<![\w$.]){n}\s*=\s*\{{", body.replace(f"let {n} =", ""))}

    out: dict[str, str] = {}
    exports_needed: dict[str, set[str]] = {f: set() for f in files}
    cross: dict[str, dict[str, set[str]]] = {f: {} for f in files}
    for f, items in files.items():
        text = "\n".join(t for _, t in items)
        for other, names in defined.items():
            if other == f:
                continue
            for n in names:
                if uses(text, n):
                    exports_needed[other].add(n)
                    cross[f].setdefault(other, set()).add(n)
        if any(uses(text, n) and n in reassigned for n in defined.get(f, set())):
            pass

    for f, items in files.items():
        parts = []
        for name, text in items:
            if name in reassigned and f != entry or name in reassigned:
                text = re.sub(rf"^let\s+{name}\s*=\s*\{{\}};", f"export const {name} = {{}};\n\n"
                              f"/** 整体换一份（ES 模块的导入方不能给 `{name}` 赋值）。 */\n"
                              f"export function reset{name[0].upper() + name[1:]}(next) {{\n"
                              f"  for (const k of Object.keys({name})) delete {name}[k];\n"
                              f"  Object.assign({name}, next);\n}}", text, flags=re.M)
            elif name in exports_needed[f] and not re.match(r"\s*(//[^\n]*\n|\s|/\*[\s\S]*?\*/)*export", text):
                text = re.sub(rf"^((?:async\s+)?(?:function\s+{re.escape(name)}\b|const\s+{re.escape(name)}\b))",
                              r"export \1", text, count=1, flags=re.M)
            parts.append(text)
        code = "\n".join(parts).strip("\n") + "\n"
        for n in reassigned:
            setter = f"reset{n[0].upper() + n[1:]}"
            # 只改赋值，不改定义那一行（`export const refs = {};`）
            code = re.sub(rf"(?<![\w$.])(?<!const ){n}\s*=\s*\{{", f"{setter}({{", code)
            # 把 resetRefs({ ... }); 的结尾补上括号
            code = re.sub(rf"({setter}\(\{{[^;]*?\}});", r"\1);", code)
        # 需要的 import
        groups: dict[str, list[str]] = {}
        for local, (srcmod, written) in imported.items():
            if uses(code, local):
                groups.setdefault(srcmod, []).append(written)
        for other, names in sorted(cross[f].items()):
            groups.setdefault("./" + other, []).extend(sorted(names))
        for n in reassigned:
            setter = f"reset{n[0].upper() + n[1:]}"
            if setter in code and n not in defined.get(f, set()):
                groups.setdefault("./" + owner[n], []).append(setter)
                exports_needed[owner[n]].add(setter)
        imp_lines = []
        for srcmod, names in groups.items():
            if any(x.startswith("* as ") for x in names):
                imp_lines.append(f"import {names[0]} from '{srcmod}';")
                continue
            one = f"import {{ {', '.join(dict.fromkeys(names))} }} from '{srcmod}';"
            if len(one) <= 100:
                imp_lines.append(one)
            else:
                names = list(dict.fromkeys(names))
                rows, cur = [], "  "
                for n in names:
                    if len(cur) + len(n) + 2 > 90:
                        rows.append(cur.rstrip())
                        cur = "  "
                    cur += n + ", "
                rows.append(cur.rstrip())
                imp_lines.append("import {\n" + "\n".join(rows) + f"\n}} from '{srcmod}';")
        doc = file_doc if f == entry else plan.get("docs", {}).get(f, "")
        out[f] = (doc.rstrip("\n") + "\n\n" if doc else "") + "\n".join(imp_lines) + "\n\n" + code
    return out


if __name__ == "__main__":
    page = Path(sys.argv[1])
    plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    for name, text in split(page, plan).items():
        (page.parent / name).write_text(text, encoding="utf-8")
        print(f"{name}: {text.count(chr(10))} 行")
