"""名字：中文路径乱码修复 + QFLS 文件名解析。

**乱码**（「中文路径可能 GBK 乱码，需统一 UTF-8」）。常见三种，都只修**显示名**，磁盘上的真名一个字不动：
  1. Linux 上列目录拿到的是 GBK 字节（Windows 拷过来的 zip、网络盘）→ Python 给的是带 surrogate 的串，
     取回原字节按 UTF-8、不行再按 GBK 解
  2. GBK 字节被当成 cp437 / latin-1 解了（zip 解压的经典问题）：「钝化剂」→「╢█╗»╝┴」
  3. UTF-8 字节被当成 GBK 解了：「中文」→「涓枃」
修不了的照原样留着，并记一笔。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_ODD = re.compile(r"[─-▟À-ÿ\u0080-¿]")   # 框线、Latin-1 —— cp437 / latin-1 错解的痕迹


def _cjk(s: str) -> int:
    return len(_CJK.findall(s))


def fix_name(name: str) -> tuple[str, str | None]:
    """返回 (修好的显示名, 用了哪种修法 / None)。拿不准就不修。"""
    if name.isascii():
        return name, None
    # 1. surrogate：原始字节没能按 UTF-8 解
    if any("\udc80" <= ch <= "\udcff" for ch in name):
        raw = os.fsencode(name)
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc), f"原始字节按 {enc.upper()} 解"
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), "原始字节解不开，不认识的字用 � 代替"
    # 2. GBK 字节被当 cp437 / latin-1 解了：原名里没有汉字、有框线 / 西欧字母
    if not _cjk(name) and _ODD.search(name):
        for wrong in ("cp437", "latin-1"):
            try:
                fixed = name.encode(wrong).decode("gbk")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if _cjk(fixed) and fixed != name:
                return fixed, f"GBK 被当成 {wrong} 解了"
    # 3. UTF-8 字节被当 GBK 解了：按 GBK 编回去能严格按 UTF-8 解出来，而且解出来还是中文
    try:
        fixed = name.encode("gbk").decode("utf-8")
        if fixed != name and _cjk(fixed) and len(fixed) < len(name):
            return fixed, "UTF-8 被当成 GBK 解了"
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name, None


# ------------------------------------------------------------------ 文件名
# 0-物镜X-405R-光强-时间戳-uuid.txt。uuid 本身带横杠，所以从两头往中间拆：时间戳是剩下的那一段
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}"
FRAME_RE = re.compile(
    rf"^0-(?P<objective>\d+(?:\.\d+)?)[xX]-(?P<laser>[^-]+)-(?P<intensity>[^-]+)-(?P<ts>.+?)-(?P<uuid>{_UUID})\.txt$")


def is_frame_file(name: str) -> bool:
    """只取 0 开头的 txt；同目录里的历史分析产物（png、csv、1- 开头的 txt……）一律不碰。"""
    return name.startswith("0") and name.lower().endswith(".txt")


def parse_intensity(s: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*sun", s.strip(), re.I)
    return float(m.group(1)) if m else None


def parse_timestamp(s: str) -> str | None:
    """几种常见写法都认，统一成 ISO 8601（不带时区 = 采集电脑的本地时间）。"""
    s = s.strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%S", "%Y%m%d-%H%M%S", "%Y%m%d_%H%M%S",
                "%Y-%m-%dT%H-%M-%S", "%Y%m%d%H%M%S%f"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    if re.fullmatch(r"\d{10}|\d{13}", s):                # Unix 时间（秒 / 毫秒）
        t = int(s) / (1000 if len(s) == 13 else 1)
        return datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return None


def parse_frame_name(name: str) -> dict:
    """拆文件名。拆不开的字段是 None，并在 problems 里说明 —— 不因为名字不规范就丢掉这一帧。"""
    m = FRAME_RE.match(name)
    if not m:
        return {"objective": None, "laser": None, "intensity": None, "intensity_sun": None,
                "timestamp": None, "timestamp_raw": None, "uuid": None,
                "problems": ["文件名不符合 0-物镜X-405R-光强-时间戳-uuid.txt"]}
    g = m.groupdict()
    problems = []
    sun = parse_intensity(g["intensity"])
    if sun is None:
        problems.append(f"光强「{g['intensity']}」认不出来")
    ts = parse_timestamp(g["ts"])
    if ts is None:
        problems.append(f"时间戳「{g['ts']}」认不出来")
    obj = g["objective"].rstrip("0").rstrip(".") if "." in g["objective"] else g["objective"]
    return {"objective": f"{obj}X", "laser": g["laser"], "intensity": g["intensity"], "intensity_sun": sun,
            "timestamp": ts, "timestamp_raw": g["ts"], "uuid": g["uuid"].lower(), "problems": problems}


SLOT_RE = re.compile(r"^slot\s*(\d+)$", re.I)
POINT_RE = re.compile(r"^point\s*(\d+)$", re.I)
HASH_RE = re.compile(r"^[0-9a-fA-F]{6,64}$")
