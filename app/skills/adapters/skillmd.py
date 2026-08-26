"""SKILL.md 通道：把一份 Claude Code 风格的 skill 文档变成可运行的 skill。

平台做的事：读文件（表格会先解析成紧凑文本）→ 把 SKILL.md 正文当系统提示 →
要求模型按 `outputs` 声明返回 JSON → 校验 → 入库。

这条通道产出的结果一律标 source='ai'、quality='review'，等人复核。
没有配模型时，skill 仍然会出现在列表里，但标为「需配置模型」，点了会明确报错，
不会假装跑通。

SKILL.md 的 frontmatter（可选，没有就用默认值）：

    ---
    id: spectrum.uvvis-reader
    name: UV-Vis 谱图读数
    category: spectrum
    version: 0.1.0
    extensions: [".csv", ".txt"]
    outputs:
      - field_name: bandgap
        label: 光学带隙
        unit: eV
      - field_name: transmittance_550
        label: 550nm 透过率
        unit: "%"
    ---
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.skills.base import (
    FileMatch, Metric, OutputSpec, ParamSpec, Skill, SkillContext, SkillResult, SkillSpec,
)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

MAX_CONTENT_CHARS = 24_000


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """极简 YAML 子集解析。只支持这份契约需要的 标量 / 列表 / 对象列表。

    不引入 PyYAML —— 依赖清单能少一个是一个，而这里的语法本来就很窄。
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    body = text[m.end():]
    data: dict[str, Any] = {}

    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue

        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()

        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            data[key] = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            i += 1
            continue
        if rest:
            data[key] = rest.strip('"').strip("'")
            i += 1
            continue

        # 缩进块：对象列表或标量列表
        items: list[Any] = []
        i += 1
        current: dict[str, Any] | None = None
        while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
            raw = lines[i]
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                if ":" in item:
                    current = {}
                    k, _, v = item.partition(":")
                    current[k.strip()] = v.strip().strip('"').strip("'")
                    items.append(current)
                else:
                    items.append(item.strip('"').strip("'"))
                    current = None
            elif current is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                current[k.strip()] = v.strip().strip('"').strip("'")
            i += 1
        data[key] = items
    return data, body


class SkillMdSkill(Skill):
    """由一份 SKILL.md 驱动的 skill。"""

    def __init__(self, spec: SkillSpec, instructions: str, source_path: Path) -> None:
        self.spec = spec
        self.instructions = instructions
        self.source_path = source_path

    # ---------------------------------------------------------------- 运行
    def run(self, ctx: SkillContext) -> SkillResult:
        from app.ai import openai_compat
        from app.ai.provider import ChatMessage, ProviderUnavailable, extract_json

        try:
            provider, model = openai_compat.resolve()
        except ProviderUnavailable as exc:
            raise RuntimeError(
                f"这个 skill 由 SKILL.md 驱动，需要一个可用模型。{exc}"
            ) from exc

        content = self._render_input(ctx)
        wanted = [o.as_dict() for o in self.spec.outputs]

        schema_hint = json.dumps(
            {"metrics": [{"field_name": o["field_name"], "value": "<数值或文本>",
                          "unit": o["unit"]} for o in wanted],
             "summary": "<一两句结论>",
             "warnings": ["<可选：数据可疑之处>"]},
            ensure_ascii=False, indent=2,
        )

        system = (
            f"{self.instructions.strip()}\n\n"
            "---\n"
            "你现在作为一个数据处理组件被调用。只输出 JSON，不要输出任何解释性文字、"
            "不要用 markdown 代码块包裹。JSON 结构必须是：\n"
            f"{schema_hint}\n"
            "metrics 里只允许出现上面列出的 field_name。数值字段必须是数字而不是字符串。"
            "无法从数据中得出的字段直接省略，不要编造。"
        )

        result = provider.chat(
            [ChatMessage("system", system), ChatMessage("user", content)],
            model=model, temperature=0.0,
            max_tokens=min(4096, 65535),
        )

        ctx.logline(f"模型：{result.provider}/{result.model}")
        try:
            payload = extract_json(result.text)
        except ValueError as exc:
            raise RuntimeError(
                f"模型没有返回可解析的 JSON。原始回复片段：{result.text[:300]}"
            ) from exc

        allowed = {o.field_name: o for o in self.spec.outputs}
        metrics: list[Metric] = []
        for item in (payload.get("metrics") or []):
            name = str(item.get("field_name", "")).strip()
            if not name:
                continue
            declared = allowed.get(name)
            if allowed and declared is None:
                ctx.logline(f"忽略未声明的字段：{name}")
                continue
            metrics.append(Metric(
                field_name=name,
                value=item.get("value"),
                unit=item.get("unit") or (declared.unit if declared else ""),
                label=(declared.label if declared else name),
                quality="review",     # 模型产出一律待复核
                source="ai",
            ))

        return SkillResult(
            metrics=metrics,
            summary=str(payload.get("summary") or ""),
            warnings=[str(w) for w in (payload.get("warnings") or [])],
            logs=ctx.log_text,
            extra={"model": f"{result.provider}/{result.model}", "channel": "skill.md"},
        )

    # ---------------------------------------------------------------- 输入渲染
    def _render_input(self, ctx: SkillContext) -> str:
        """把文件变成模型能读的文本。表格先解析成紧凑格式，省 token 也更准。"""
        from app.parsers import sniff

        chunks: list[str] = []
        for ref in ctx.files:
            chunks.append(f"### 文件：{ref.display_path or ref.filename}")
            if ref.sample_name:
                chunks.append(f"样品：{ref.sample_name}")
            suffix = ref.ext.lower()
            if suffix in sniff.TEXT_EXT or suffix in sniff.EXCEL_EXT:
                try:
                    df, s = sniff.load_frame(ref.path, max_rows=400)
                    if getattr(s, "preamble", None):
                        chunks.append("文件抬头：\n" + "\n".join(s.preamble[:15]))
                    chunks.append(f"列：{list(df.columns)}  共 {len(df)} 行（最多展示 400 行）")
                    chunks.append(df.head(400).to_csv(index=False))
                except Exception:
                    text, _ = sniff.read_text(ref.path, max_bytes=40_000)
                    chunks.append(text)
            else:
                try:
                    text, _ = sniff.read_text(ref.path, max_bytes=20_000)
                    chunks.append(text)
                except Exception as exc:
                    chunks.append(f"（无法作为文本读取：{exc}）")

        if ctx.params:
            chunks.append(f"### 用户给的参数\n{json.dumps(ctx.params, ensure_ascii=False)}")

        out = "\n\n".join(chunks)
        if len(out) > MAX_CONTENT_CHARS:
            out = out[:MAX_CONTENT_CHARS] + "\n…（内容过长已截断）"
        return out


def load(path: Path, origin: str = "user") -> SkillMdSkill:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    folder = path.parent.name
    outputs = []
    for o in (meta.get("outputs") or []):
        if isinstance(o, dict):
            outputs.append(OutputSpec(
                field_name=o.get("field_name") or o.get("name") or "value",
                label=o.get("label", ""),
                unit=o.get("unit", ""),
                kind=o.get("kind", "number"),
            ))
        elif isinstance(o, str):
            outputs.append(OutputSpec(field_name=o, label=o))

    params = []
    for p in (meta.get("params") or []):
        if isinstance(p, dict):
            params.append(ParamSpec(
                key=p.get("key") or p.get("name") or "param",
                label=p.get("label", p.get("key", "参数")),
                type=p.get("type", "text"),
                default=p.get("default"),
                unit=p.get("unit", ""),
                help=p.get("help", ""),
            ))

    from app.ai import openai_compat

    has_model = bool(openai_compat.list_models())

    spec = SkillSpec(
        id=meta.get("id") or f"md.{folder}",
        name=meta.get("name") or folder,
        category=meta.get("category") or "other",
        version=str(meta.get("version") or "0.1.0"),
        accepts=FileMatch(
            extensions=meta.get("extensions") or (),
            filename_globs=meta.get("globs") or (),
            content_keywords=meta.get("keywords") or (),
        ),
        params=params,
        outputs=outputs,
        description=(meta.get("description") or _first_paragraph(body)),
        origin="skill.md",
        ready=has_model,
        ready_note="" if has_model else "由 SKILL.md 驱动，需要先在「设置 → 模型」配置一个模型",
    )
    skill = SkillMdSkill(spec, body, path)
    if origin == "builtin":
        skill.spec = replace(spec, origin="skill.md")
    return skill


def _first_paragraph(body: str) -> str:
    for block in body.strip().split("\n\n"):
        t = block.strip()
        if t and not t.startswith("#"):
            return t[:240]
    return ""
