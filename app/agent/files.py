"""智能体碰文件 —— 路径约束、打码、改前备份、改动记录、回滚（照 IFlow 的 lib/server/serverFs.ts）。

能碰哪些地方由「文件夹」名单决定（智能体页右侧可以加、可以删）：
  · 工作区（workspace/）总在名单里、可写 —— 但里面这几处**只读**：数据库文件、raw/（内容寻址的原始数据复制件，
    改了就和库里记的 sha256 对不上）、tables/、derived/、cache/（平台自己生成的）、config/（密钥）、
    modules/（同事的功能模块 —— 往里写 Python 等于让平台下次重载时执行它）、备份目录
  · 你加的文件夹：每个单独选「只读」或「读写」
所有路径都解析成真实路径（含软链）再核一次在不在名单里，`../` 和软链都逃不出去。相对路径从工作区算。

读出来的内容先打码再给模型（模型那头是外部网关）：配置里的 apiKey、`sk-…` 这类密钥、`Bearer …`、URL 里的口令、私钥块。
写之前把原文件拷到 workspace/agent_backups/<改动 id>/，agent_file_change 记前后 sha；回滚先核对当前文件就是这次改出来的那份。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from app import config
from app.parsers.sniff import decode_bytes
from app.storage import db

BACKUP_DIR = "agent_backups"
# 工作区里只读的那几处（第一层名字）
PROTECTED = {"hte.db", "hte.db-wal", "hte.db-shm", "hte.db-journal", "raw", "tables", "derived", "cache",
             "config", "modules", "backups", BACKUP_DIR}
# 列目录 / 搜索时默认跳过的（体积大、没信息量）；直接给路径仍可读
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "cache", BACKUP_DIR}

READ_MAX_BYTES = 20 * 1024 * 1024
WRITE_MAX_CHARS = 2 * 1024 * 1024
GREP_FILE_MAX = 2 * 1024 * 1024
LIST_MAX = 500


class FileToolError(ValueError):
    pass


# ------------------------------------------------------------------ 名单
def roots() -> list[dict]:
    """[{path, write, builtin}]，工作区在第一个。你加的那些存在设置 agent.roots 里。"""
    out = [{"path": str(config.WORKSPACE), "write": True, "builtin": True}]
    for r in db.get_setting("agent.roots", []) or []:
        if isinstance(r, dict) and r.get("path"):
            out.append({"path": str(Path(r["path"])), "write": bool(r.get("write")), "builtin": False})
    return out


def check_new_root(path: str) -> str:
    """加文件夹之前的检查：要存在、是目录，不许是整个盘 / 家目录本身（太宽了）。"""
    p = Path(os.path.expanduser((path or "").strip()))
    if not p.is_absolute():
        raise FileToolError("要写完整路径（比如 D:\\数据\\2026 或 /home/me/data）")
    p = Path(os.path.realpath(p))
    if not p.is_dir():
        raise FileToolError(f"这个文件夹不存在：{p}")
    if p == Path(p.anchor) or p == Path(os.path.realpath(Path.home())):
        raise FileToolError(f"{p} 太宽了（整个盘或你的家目录）—— 选一个具体的文件夹")
    return str(p)


def _within(x: Path, base: Path) -> bool:
    return x == base or base in x.parents


def resolve(path: str, *, write: bool = False) -> Path:
    """路径 → 绝对路径；不在名单里（含经软链跑出去）、或要写一个只读的地方，都拒绝。"""
    raw = (path or "").strip() or "."
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = config.WORKSPACE / p
    p = Path(os.path.normpath(p))
    # 已存在的部分按真实路径再核一次（软链）
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    real = Path(os.path.realpath(probe)) / p.relative_to(probe)
    hit = None
    for r in roots():
        base = Path(os.path.realpath(r["path"]))
        if _within(real, base):
            hit = (r, base)
            break
    if not hit:
        names = "、".join(r["path"] for r in roots())
        raise FileToolError(f"{raw} 不在能碰的文件夹里（{names}）。要用别的文件夹，请用户在智能体页右侧加上")
    r, base = hit
    if write:
        if not r["write"]:
            raise FileToolError(f"{r['path']} 是只读的（用户加这个文件夹时没给写权限）")
        if r["builtin"] and real != base:
            first = real.relative_to(base).parts[0]
            if first in PROTECTED:
                raise FileToolError(f"工作区里的 {first} 是只读的（{_why(first)}）")
    return real


def _why(first: str) -> str:
    return {"raw": "原始数据的复制件，改了就和库里记的 sha256 对不上", "config": "放着密钥",
            "modules": "同事的功能模块 —— 往里写代码等于让平台下次重载时执行它",
            "tables": "平台生成的数值表", "derived": "平台生成的图", "cache": "平台的缓存",
            BACKUP_DIR: "智能体改文件前的备份", "backups": "数据库升级前的备份"}.get(first, "平台的数据库文件")


def shown(p: Path) -> str:
    """给人和模型看的路径：工作区里的写相对路径，外面的写绝对路径。"""
    ws = Path(os.path.realpath(config.WORKSPACE))
    try:
        rel = p.relative_to(ws)
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return str(p)


# ------------------------------------------------------------------ 打码
_MASK_KEY = re.compile(r'("?(?:api[_-]?key|apikey|token|secret|password|passwd|access[_-]?key|private[_-]?key)"?\s*[:=]\s*)'
                       r'("[^"]*"|\'[^\']*\'|[^\s,}]+)', re.I)


def _mask(v: str) -> str:
    v = v.strip().strip("\"'")
    return "****" if len(v) <= 4 else f"{v[:2]}****（{len(v)} 字符）"


def redact(text: str) -> str:
    """读出来的东西先打码再给模型 —— 模型那头是外部网关，密钥不能出去。"""
    s = _MASK_KEY.sub(lambda m: f'{m.group(1)}"{_mask(m.group(2))}"', text)
    s = re.sub(r"\b(sk|pk|rk|ak|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}", lambda m: _mask(m.group(0)), s)
    s = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", "Bearer ****", s)
    s = re.sub(r"(\w+://[^:\s/]+:)([^@\s]+)(@)", r"\1****\3", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
               "-----BEGIN PRIVATE KEY----- **** -----END PRIVATE KEY-----", s)
    return s


# ------------------------------------------------------------------ 读
def _decode(raw: bytes) -> tuple[str | None, str]:
    """(文本, 编码)；二进制返回 (None, '')。UTF-16 满是 NUL，不能一见 NUL 就当二进制 —— 先认它。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return decode_bytes(raw)
    if b"\x00" in raw[:8000]:
        return decode_bytes(raw) if _looks_utf16(raw) else (None, "")
    return decode_bytes(raw)


def _looks_utf16(raw: bytes) -> bool:
    body = raw[: min(len(raw), 4096) // 2 * 2]
    for enc in ("utf-16-le", "utf-16-be"):
        try:
            t = body.decode(enc)
        except UnicodeDecodeError:
            continue
        if t and sum(ch.isprintable() or ch in "\r\n\t" for ch in t) > 0.95 * len(t):
            return True
    return False


def list_dir(path: str = ".", depth: int = 1, show_all: bool = False) -> dict:
    base = resolve(path)
    if not base.exists():
        raise FileToolError(f"不存在：{path}")
    if not base.is_dir():
        raise FileToolError(f"{path} 不是文件夹（读文件用 read_file）")
    depth = max(1, min(int(depth), 4))
    out: list[dict] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda x: x.name.lower())
        except PermissionError:
            out.append({"path": shown(d), "type": "dir", "note": "没有权限读"})
            return
        for e in entries:
            if len(out) >= LIST_MAX:
                return
            if not show_all and level > 1 and e.name in SKIP_DIRS:
                continue
            try:
                st = e.lstat()
            except OSError:
                continue
            kind = "link" if e.is_symlink() else "dir" if e.is_dir() else "file"
            item = {"path": shown(e), "type": kind,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes")}
            if kind == "file":
                item["size"] = st.st_size
            out.append(item)
            if kind == "dir" and level < depth and (show_all or e.name not in SKIP_DIRS):
                walk(e, level + 1)

    walk(base, 1)
    return {"dir": shown(base), "entries": out, **({"truncated": True} if len(out) >= LIST_MAX else {})}


def read_text(path: str, offset: int = 0, limit: int = 400) -> dict:
    p = resolve(path)
    if not p.exists():
        raise FileToolError(f"不存在：{path}")
    if p.is_dir():
        raise FileToolError(f"{path} 是文件夹，用 list_files")
    size = p.stat().st_size
    if size > READ_MAX_BYTES:
        raise FileToolError(f"{path} 有 {size / 1e6:.1f} MB，太大；用 search_files 找要看的部分")
    text, enc = _decode(p.read_bytes())
    if text is None:
        return {"path": shown(p), "size": size, "binary": True, "note": "二进制文件，不显示内容"}
    # 先打码再加行号：行号前缀会让 KEY=VALUE 认不出来
    lines = redact(text).splitlines()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 2000))
    body = "\n".join(f"{offset + i + 1:>5}  {line}" for i, line in enumerate(lines[offset:offset + limit]))
    if len(body) > 60000:
        body = body[:60000] + "\n…（这一段超过 6 万字，截断了；缩小 limit 分段读）"
    end = min(len(lines), offset + limit)
    return {"path": shown(p), "size": size, "encoding": enc, "lines": len(lines), "from": offset + 1, "to": end,
            **({"next_offset": end} if end < len(lines) else {}), "content": body}


def _glob_re(glob: str) -> re.Pattern:
    g = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*").replace(r"\?", ".")
    return re.compile(f"^{g}$", re.I)


def grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False, max_hits: int = 100) -> dict:
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as exc:
        raise FileToolError(f"正则写错了：{exc}") from exc
    base = resolve(path)
    if not base.exists():
        raise FileToolError(f"不存在：{path}")
    gre = _glob_re(glob) if glob else None
    max_hits = max(1, min(int(max_hits), 500))
    hits: list[dict] = []
    scanned = 0

    def scan(f: Path) -> None:
        nonlocal scanned
        try:
            if f.stat().st_size > GREP_FILE_MAX:
                return
            text, _ = _decode(f.read_bytes())
        except OSError:
            return
        if text is None:
            return
        scanned += 1
        for i, line in enumerate(text.splitlines()):
            if len(hits) >= max_hits:
                return
            if rx.search(line):
                hits.append({"path": shown(f), "line": i + 1, "text": redact(line)[:300]})

    if base.is_file():
        scan(base)
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if len(hits) >= max_hits:
                    break
                f = Path(dirpath) / name
                rel = shown(f).replace(os.sep, "/")
                if gre and not (gre.match(rel) or gre.match(name)):
                    continue
                scan(f)
    return {"pattern": pattern, "scanned_files": scanned, "hits": hits,
            **({"truncated": True} if len(hits) >= max_hits else {})}


# ------------------------------------------------------------------ 写 + 备份 + 回滚
def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def brief_diff(a: str, b: str, max_lines: int = 30) -> str:
    """极简行级差异（给模型和审批卡看）：只列变了的那一段（照 IFlow 的 briefDiff）。"""
    A, B = a.split("\n"), b.split("\n")
    i = 0
    while i < len(A) and i < len(B) and A[i] == B[i]:
        i += 1
    j = 0
    while j < len(A) - i and j < len(B) - i and A[len(A) - 1 - j] == B[len(B) - 1 - j]:
        j += 1
    dele, add = A[i:len(A) - j], B[i:len(B) - j]
    out = [f"@@ 第 {i + 1} 行起：删 {len(dele)} 行、加 {len(add)} 行"]
    out += [f"- {x}" for x in dele[:max_lines]] + [f"+ {x}" for x in add[:max_lines]]
    if len(dele) > max_lines or len(add) > max_lines:
        out.append("…（更多改动省略）")
    return redact("\n".join(out))


def plan_write(path: str, content: str | None = None, find: str | None = None,
               replace: str | None = None) -> tuple[Path, bytes | None, str, str]:
    """算出要写成什么样（不写）。返回 (绝对路径, 原字节或 None, 新文本, 编码)。预览和真正写都走这里。"""
    p = resolve(path, write=True)
    if p.exists() and p.is_dir():
        raise FileToolError(f"{path} 是文件夹")
    old = p.read_bytes() if p.exists() else None
    enc = "utf-8"
    old_text = ""
    if old is not None:
        t, e = _decode(old)
        if t is None:
            raise FileToolError(f"{path} 是二进制文件，不能按文本改")
        old_text, enc = t, (e or "utf-8")
    if find is not None:
        if old is None:
            raise FileToolError(f"{path} 不存在，不能按片段替换；要新建文件请给 content")
        n = old_text.count(find)
        if n != 1:
            raise FileToolError("find 的原文在文件里找不到（要逐字一致，含空白与缩进）；先 read_file 再改" if n == 0
                                else f"find 的原文出现了 {n} 次，不唯一；多带几行上下文")
        new = old_text.replace(find, replace or "", 1)
    elif content is not None:
        new = content
    else:
        raise FileToolError("要么给 content（整个文件），要么给 find + replace（改一段）")
    if len(new) > WRITE_MAX_CHARS:
        raise FileToolError(f"新内容超过 {WRITE_MAX_CHARS // 1024 // 1024} MB 字符，太大")
    if old is not None and old_text == new:
        raise FileToolError("改完和原文一模一样，没有改动")
    return p, old, new, enc


def preview_write(path: str, content: str | None = None, find: str | None = None,
                  replace: str | None = None, note: str = "") -> str:
    p, old, new, _ = plan_write(path, content, find, replace)
    old_text = _decode(old)[0] if old is not None else ""
    head = f"{'改' if old is not None else '新建'}文件 {shown(p)}" + (f" —— {note}" if note else "")
    tail = "改之前会自动备份，可以撤销。" if old is not None else "新建的文件，撤销 = 删掉它。"
    return f"{head}\n{tail}\n{brief_diff(old_text or '', new)}"


def write(path: str, content: str | None = None, find: str | None = None, replace: str | None = None,
          note: str = "", session_id: str | None = None) -> dict:
    p, old, new, enc = plan_write(path, content, find, replace)
    change_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    backup = None
    if old is not None:
        bk = config.WORKSPACE / BACKUP_DIR / change_id / p.name
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bk)
        backup = str(bk)
    try:
        data = new.encode(enc if enc not in ("latin-1",) else "utf-8")
    except (UnicodeEncodeError, LookupError):
        data = new.encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.agent-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)                     # 原子替换：写到一半断电也不会留下半个文件
    with db.tx() as c:
        c.execute("INSERT INTO agent_file_change(change_id, session_id, path, existed, before_sha, after_sha,"
                  " backup, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (change_id, session_id, str(p), int(old is not None), _sha(old) if old is not None else None,
                   _sha(data), backup, (note or "")[:300] or None, db.now()))
    old_text = _decode(old)[0] if old is not None else ""
    return {"change_id": change_id, "path": shown(p), "created": old is None, "bytes": len(data),
            "backup": shown(Path(backup)) if backup else "（新建文件，撤销 = 删掉）",
            "diff": brief_diff(old_text or "", new), "undo": f"rollback_file(change_id=\"{change_id}\")"}


def changes(session_id: str | None = None, path: str | None = None, limit: int = 30) -> list[dict]:
    where, params = [], []
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if path:
        where.append("path = ?")
        params.append(str(resolve(path)))
    rows = db.query("SELECT * FROM agent_file_change" + (" WHERE " + " AND ".join(where) if where else "")
                    + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, max(1, min(int(limit), 200))))
    return [{"change_id": r["change_id"], "path": shown(Path(r["path"])), "at": r["created_at"],
             "created": not r["existed"], "note": r["note"], "rolled_back": r["rolled_back_at"]} for r in rows]


def rollback(change_id: str | None = None, session_id: str | None = None, force: bool = False) -> dict:
    """撤销一次改动（或这个会话的全部改动，从新到旧）。当前文件已经不是这次改出来的那份就拒绝，除非 force。"""
    if change_id:
        rows = db.query("SELECT * FROM agent_file_change WHERE change_id = ?", (change_id,))
    elif session_id:
        rows = db.query("SELECT * FROM agent_file_change WHERE session_id = ? AND rolled_back_at IS NULL"
                        " ORDER BY created_at DESC, rowid DESC", (session_id,))
    else:
        raise FileToolError("给 change_id（撤销一次）或 session_id（撤销这次对话的全部改动）")
    if not rows:
        raise FileToolError("没找到这次改动")
    restored, skipped = [], []
    for r in rows:
        p = Path(r["path"])
        if r["rolled_back_at"]:
            skipped.append(f"{shown(p)}（已经撤销过）")
            continue
        cur = _sha(p.read_bytes()) if p.exists() else None
        if cur != r["after_sha"] and not force:
            skipped.append(f"{shown(p)}（改完之后又被改过，现在的内容不是这次改出来的；要强制撤销加 force）")
            continue
        if r["existed"] and r["backup"] and Path(r["backup"]).exists():
            shutil.copy2(r["backup"], p)
        elif not r["existed"] and p.exists():
            p.unlink()
        with db.tx() as c:
            c.execute("UPDATE agent_file_change SET rolled_back_at = ? WHERE change_id = ?", (db.now(), r["change_id"]))
        restored.append(shown(p))
    return {"restored": restored, "skipped": skipped}
