"""模块计算结果的内存缓存（C078）。

样品页每进一次都要把每个模块全分辨率算一遍。膜厚那个一次 300 多 ms（算法本身是冻结规范，不动），
而同一个样品、同一套参数，算出来永远一样 —— 回到同一个样品页、上一个 / 下一个来回翻时，没必要再算。

钥匙 = (模块 id, 模块版本, 文件 id, 文件内容的 sha256, 这次的参数, 只算哪几个面板)。
文件 id 也在里面：模块的输出里可能带着按文件 id 拼的图片地址（ctx.image_url）。
  - 文件内容变了（原地引用的文件被仪器软件改过）→ sha 变了 → 自然算新的
  - 模块重新加载（装 / 卸 / 升级 / 改了代码点「重载」）→ registry.load_all 清空整个缓存（版本号没改、代码改了也不会拿到旧结果）
只存在内存里，最多 64 份（一份是几个面板的曲线，几十 KB）；重启就没了。
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

MAX_ENTRIES = 64

_lock = threading.Lock()
_store: OrderedDict[str, dict] = OrderedDict()


def key(module_id: str, version: str, artifact_id: str, content_sha: str, params: dict,
        only: list[str] | None) -> str | None:
    """算不出稳定钥匙（没有内容 sha、参数里有不能序列化的东西）就返回 None —— 那就不缓存，照算。"""
    if not content_sha:
        return None
    try:
        p = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return json.dumps([module_id, version, artifact_id, content_sha, p, sorted(only) if only is not None else None])


def get(k: str | None) -> dict | None:
    if k is None:
        return None
    with _lock:
        hit = _store.get(k)
        if hit is not None:
            _store.move_to_end(k)
        return hit


def put(k: str | None, value: dict) -> None:
    if k is None:
        return
    with _lock:
        _store[k] = value
        _store.move_to_end(k)
        while len(_store) > MAX_ENTRIES:
            _store.popitem(last=False)


def clear() -> None:
    with _lock:
        _store.clear()
