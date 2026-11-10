"""请求体模型：每个 POST / PATCH 接口收什么，一处写清。

原来请求体全是 `payload: dict`，`/api/docs` 里看不出参数，拼错一个键（`fitler`）静悄悄地被当成没给（C075）。现在：
  - 每个字段的类型、默认值、说明都在这里，`/api/docs` 和 `docs/REFERENCE.md` 照着出
  - **不认识的键回 400**（「不认识的参数：fitler」）—— 拼错不再静悄悄
  - 类型不对回 400，说的是中文（app/main.py 的 bad_request）

路由里第一行 `payload = body.model_dump(exclude_unset=True)`：只留**调用方真给了的键**，下面的逻辑和原来一样
（有几处靠「给没给这个键」区分「不改」和「清空」，比如改会话标题、改智能体设置）。所以这里的默认值是
**写给人看的** —— 和路由里实际用的默认值保持一致，改一处要改另一处。

筛选式（filter）、配方（recipe）、范围（scope）、QFLS 参数覆盖（params）这几样内部有自己的校验
（selection.normalize / batch.Recipe / chat._clean_scope / qfls.pipeline.merge_params），这里只说它是一组键值。
设置（POST /api/settings）是唯一还收任意键的：能改哪些键由 GET /api/settings 的 editable 给出，路由自己核对。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


FILTER = "筛选式，见 docs/REFERENCE.md「筛选式」；不给 = 全部样品"
Json = dict[str, Any]


# ---------------------------------------------------------------- 导入（/api/files）
class ScanBody(Req):
    path: str | None = Field(None, description="本机文件夹或文件的路径（必填）")
    mode: str = Field("files", description="files = 按文件名走命名规则；folders = 每个含 Data.csv 的子文件夹算一次测量")
    recursive: bool = Field(True, description="files 模式下是否进子文件夹")


class ImportBody(Req):
    files: list[Json] = Field(default_factory=list, description="扫描预览的行（可能被人改过），原样传回")
    source_hint: str = Field("", description="来源备注，记进导入批次")


class NamingPreviewBody(Req):
    paths: list[str] = Field(default_factory=list, description="要试的文件路径 / 文件名")
    rules: list[Any] | None = Field(None, description="命名规则；不给 = 用设置里存的那套")


# ---------------------------------------------------------------- 功能模块（/api/modules）
class ModuleValidateBody(Req):
    module_id: str = Field("", description="已装模块的 id")


class ModuleComputeBody(Req):
    artifact_id: str = Field("", description="矩阵文件的 artifact_id（必填）")
    params: Json = Field(default_factory=dict, description="控件取值；没给的用模块声明的默认值")
    changed: list[str] | None = Field(None, description="这次动过的控件 key；给了就只重算受影响的面板")


# ---------------------------------------------------------------- QFLS（/api/qfls）
class QflsScanBody(Req):
    root: str = Field("", description="数据根目录")


class QflsStartBody(Req):
    root: str | None = Field(None, description="数据根目录（demo=true 时不用给）")
    demo: bool = Field(False, description="true = 先生成演示数据（假想）再处理")
    full: bool = Field(False, description="演示数据：true = 按全尺寸生成（慢）")
    title: str | None = Field(None, description="这次处理的名字；不给 = 文件夹名")
    figures: bool = Field(True, description="是否出图（不出图快很多）")
    params: Json = Field(default_factory=dict, description="阈值覆盖，形状同 GET /api/qfls/method 的 params；嵌套的深合并")


# ---------------------------------------------------------------- 构效关系（/api/relation）
class RelationPairsBody(Req):
    x: str = Field("", description="X 字段（GET /api/results/fields 里的 key，必填）")
    y: str = Field("", description="Y 字段（必填）")
    filter: Json | None = Field(None, description=FILTER)


class RelationGroupsBody(Req):
    y: str = Field("", description="要看分布的字段（必填）")
    by: str = Field("batch", description="按什么分组")
    filter: Json | None = Field(None, description=FILTER)


# ---------------------------------------------------------------- 关键结果（/api/results）
class QualityBody(Req):
    quality: str = Field("", description="validated / review / reject")
    note: str = Field("", description="为什么这么标")


class QualityManyBody(QualityBody):
    ids: list[int] = Field(default_factory=list, description="key_result 的 id；重复的只算一次，一次最多 500 条")


# ---------------------------------------------------------------- 选择与样品集（/api/selection）
class FilterBody(Req):
    filter: Json | None = Field(None, description=FILTER)


class SelectionQueryBody(FilterBody):
    limit: int = Field(100, description="一页多少个（最多 500）")
    offset: int = Field(0, description="从第几个开始")
    order: str = Field("name", description="排序")


class SuggestBody(FilterBody):
    sample_ids: list[str] = Field(default_factory=list, description="手选的几个样品（少于 2 个不提议）")


class IdsBody(FilterBody):
    limit: int = Field(5000, description="最多展开多少个（上限 50000）")


class CreateSetBody(Req):
    name: str = Field("", description="样品集名字")
    kind: str = Field("dynamic", description="dynamic = 存筛选式（样品会变）；pinned = 存名单（钉死）")
    filter: Json | None = Field(None, description=FILTER)
    sample_ids: list[str] | None = Field(None, description="pinned 时的名单；不给就按 filter 当场展开")
    note: str = Field("", description="备注")


class RenameBody(Req):
    name: str = Field("", description="新名字")


# ---------------------------------------------------------------- 批处理（/api/batch）
class BatchRunBody(Req):
    filter: Json | None = Field(None, description=FILTER)
    set_id: str | None = Field(None, description="按样品集跑：给了就用它的筛选式 / 名单，filter 不再看")
    recipe: Json | None = Field(None, description="配方（控件取值）；空 = 已装模块声明的默认配方，见 /api/batch/preview")
    title: str = Field("", description="这次运行的名字")


# ---------------------------------------------------------------- AI 抽屉（/api/chat）
class ModelChoice(Req):
    provider: str | None = Field(None, description="用哪个模型网关；不给 = 设置里的默认")
    model: str | None = Field(None, description="用哪个模型；不给 = 设置里的默认")


class NewConversationBody(Req):
    title: str | None = Field(None, description="不给 = 「新对话」")
    scope: Json | None = Field(None, description="范围 {mode, filter, label, topic, run_id}")


class PatchConversationBody(Req):
    title: str | None = Field(None, description="给了才改")
    scope: Json | None = Field(None, description="给了才改")


class ScopePreviewBody(Req):
    scope: Json | None = Field(None, description="范围 {mode, filter, label, topic, run_id}")


class MessageBody(ModelChoice):
    content: str = Field("", description="问题（必填）")
    scope: Json | None = Field(None, description="这次的范围；不给 = 沿用会话上存的")
    spot: str | None = Field(None, description="术语 ⓘ：眼前这处读数的摘要，存进这条消息")
    context_note: str | None = Field(None, description="术语 ⓘ：定义原文 + 附加段 + 眼前读数，只进 system、不进会话")


class PinBody(Req):
    analysis_run_id: str = Field("", description="钉到哪一次对比（必填）")
    note: str = Field("", description="分析正文（必填，存快照）")
    conversation_id: str | None = Field(None, description="出自哪个对话")
    message_id: str | None = Field(None, description="出自哪条回答")


# ---------------------------------------------------------------- 智能体（/api/agent）
class AgentSettingsBody(Req):
    permission: str | None = Field(None, description="权限档位；给了才改")
    commands: bool | None = Field(None, description="能不能跑命令；给了才改")
    driver: str | None = Field(None, description="驱动方式；给了才改")
    roots: list[Any] | None = Field(None, description="工作区之外能碰的文件夹 [{path, write}]，最多 20 个；给了就整张换掉")


class RollbackBody(Req):
    force: bool = Field(False, description="文件在那之后又被改过也照样恢复")


class AgentRunBody(ModelChoice):
    prompt: str = Field("", description="要做的事（必填，最多 4000 字）")
    session_id: str | None = Field(None, description="接着哪个会话说；不给 = 新建")
    run_mode: str = Field("exec", description="exec = 直接执行；plan = 先出计划")
    plan: Any = Field(None, description="批准的计划 {goal, steps, risks, questions}：给了就按它执行")


class AgentResumeBody(ModelChoice):
    decisions: Json = Field(default_factory=dict, description="对每个待批工具调用的决定 {调用 id: \"approve\" | 别的 = 拒绝}")
    remember: list[Any] = Field(default_factory=list, description="以后不用再问的工具名")


# ---------------------------------------------------------------- 设置 / 模型（/api/settings）
class PresetBody(Req):
    name: str = Field("", description="显示名")
    base_url: str = Field("", description="接口地址")
    api: str = Field("openai-completions", description="协议")


class DiscoverBody(Req):
    base_url: str = Field("", description="接口地址")
    api_key: str = Field("", description="密钥；留空 = 用这个地址已经存着的")
    api: str = Field("openai-completions", description="协议")


class TestOneModelBody(DiscoverBody):
    model: str = Field("", description="模型名")


class SaveModelsBody(Req):
    config: Json | str | None = Field(None, description="providers 配置：JSON 对象，或者它的文本")


class SimpleModelBody(Req):
    name: str = Field("我的模型", description="显示名")
    base_url: str = Field("", description="接口地址（必填，http:// 或 https:// 开头）")
    api_key: str = Field("", description="密钥；留空 = 不改")
    api: str = Field("openai-completions", description="协议")
    thinking: str | None = Field(None, description="思考档位")
    model_ids: list[str] | None = Field(None, description="一次存几个模型")
    model_id: str | None = Field(None, description="老写法：只存一个")


class TestModelsBody(ModelChoice):
    pass


def dump(body: Req | None) -> dict:
    """只留调用方真给了的键（见文件头）。可选的请求体没给时是 {}。"""
    return body.model_dump(exclude_unset=True) if body is not None else {}
