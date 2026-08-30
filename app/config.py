"""运行期路径与全局配置。

工作区（workspace）是这个应用的全部持久化状态：数据库、复制进来的原始文件、
派生产物、用户自带的 skill、本地模型配置。整个目录可以打包带走或备份。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "HTE Studio"
APP_VERSION = "0.1.0"

# 仓库根目录（app/config.py 的上两级）
ROOT = Path(__file__).resolve().parent.parent

# 工作区可以用环境变量搬到别处，例如放到 D: 盘或网络盘
WORKSPACE = Path(os.environ.get("HTE_WORKSPACE", ROOT / "workspace")).resolve()

DB_PATH = WORKSPACE / "hte.db"
RAW_DIR = WORKSPACE / "raw"          # 复制进来的文本类原始文件（按 sha256 分桶）
DERIVED_DIR = WORKSPACE / "derived"  # 缩略图、skill 产出的图
TABLES_DIR = WORKSPACE / "tables"    # Parquet 数值表
SKILLS_DIR = WORKSPACE / "skills"    # 用户拖进来的 skill
MODULES_DIR = WORKSPACE / "modules"  # 同事写的功能模块（拖进来即生效）
CONFIG_DIR = WORKSPACE / "config"    # providers.json 等本地配置（含密钥，不入库）
LOGS_DIR = WORKSPACE / "logs"
TMP_DIR = WORKSPACE / "tmp"

WEB_DIR = ROOT / "web"
BUILTIN_SKILLS_DIR = ROOT / "app" / "skills" / "builtin"
BUILTIN_MODULES_DIR = ROOT / "app" / "modules" / "builtin"
PROVIDERS_PATH = CONFIG_DIR / "providers.json"
PROVIDERS_EXAMPLE_PATH = ROOT / "config" / "providers.example.json"

# AI 抽屉一次最多把多少个样品的逐条明细喂给模型。
# 超过就只给汇总，并要求模型先提一个收窄的筛选式让用户确认 ——
# 与其让它对着 200 个样品的截断列表编一个「综合来看」，不如多问一轮。
AI_DETAIL_MAX = int(os.environ.get("HTE_AI_DETAIL_MAX", "40"))

HOST = os.environ.get("HTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("HTE_PORT", "8765"))

ALL_DIRS = (WORKSPACE, RAW_DIR, DERIVED_DIR, TABLES_DIR, SKILLS_DIR, MODULES_DIR,
            CONFIG_DIR, LOGS_DIR, TMP_DIR)


def ensure_dirs() -> None:
    """创建工作区目录树。幂等。"""
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)
    (DERIVED_DIR / "thumbs").mkdir(exist_ok=True)
    (DERIVED_DIR / "figures").mkdir(exist_ok=True)


@dataclass(frozen=True)
class Defaults:
    """首次启动写入 app_setting 的默认值。用户可在设置页改。"""

    # 分类落地策略：文本类复制进工作区，图像类原地引用
    copy_extensions: tuple[str, ...] = (
        ".csv", ".txt", ".dat", ".tsv", ".json", ".xlsx", ".xls",
        ".xml", ".log", ".md", ".yaml", ".yml", ".ini", ".asc", ".spc",
    )
    reference_extensions: tuple[str, ...] = (
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif",
    )
    # 既不在复制名单也不在引用名单的扩展名，默认怎么办
    unknown_policy: str = "reference"  # "reference" | "copy"

    # 文件名 → sample_id 的解析规则，按顺序匹配，第一条命中为准
    naming_rules: tuple[str, ...] = (
        "{batch}_{sample}_{method}",
        "{batch}_{sample}",
        "{sample}",
    )

    thumbnail_max_px: int = 512
    max_preview_rows: int = 5000

    # 解析缓存的容量上限。上千个样品的 npz 能堆到几个 GB。
    # 缓存是内容寻址的，超了就淘汰最久没用的，删了只是下次慢约 0.9 秒。
    cache_limit_gb: float = 8.0


DEFAULTS = Defaults()
