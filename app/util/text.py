"""字节 → 文本。全平台只有这一份编码判断（原来解析器、QFLS 的 qvdf、智能体跑命令的输出各写一份，规则不一样）。"""
from __future__ import annotations

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "latin-1")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """字节 → (文本, 编码)。**永不抛错**（latin-1 兜底）。

    UTF-16 必须**先**认出来，而且不能靠「试着解一下看报不报错」：
    latin-1 对任何字节序列都不报错，所以候选列表里只要有它，
    UTF-16 文件就会被「成功」解成每个字符后面跟一个 NUL 的乱码 ——
    不崩、不报错，只是后面所有的字符串比较都对不上。
    Windows 上的仪器软件（.NET / LabVIEW）写 UTF-16LE 很常见。
    中文 Windows 上命令行输出是 GBK（cp936）—— GB18030 是它的超集，也认得。
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # 截断读取可能切在半个码元上，砍成偶数长度再解
        try:
            return raw[: len(raw) // 2 * 2].decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    probe = raw[:4096]
    if probe and probe.count(0) > len(probe) // 4:      # 一半字节是 NUL = 十有八九是 UTF-16
        body = raw[: len(raw) // 2 * 2]
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return body.decode(enc), enc
            except UnicodeDecodeError:
                continue
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def decode_text(raw: bytes) -> str:
    return decode_bytes(raw)[0]
