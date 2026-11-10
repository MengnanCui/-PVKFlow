"""AI 助手（右侧抽屉）和 ⓘ 浮层的「怎么跟模型说话」：系统提示、术语提示、从回答里抠动作卡片。

原来写在路由文件 app/api/chat.py 里（C074 挪出来）：提示词和卡片规则是 AI 的业务逻辑，
路由只管会话、流式、钉住。改提示词、加一种动作都在这里。
"""
from __future__ import annotations

from app.ai.provider import extract_json

# 模型可以吐的动作。写死成白名单 —— 模型编一个 "delete_everything" 出来时，
# 界面应该当它没说，而不是照着渲染一个按钮。
ACTIONS = ("none", "narrow", "select", "process")

SYSTEM_PROMPT = """\
你是高通量实验数据平台 HTE Studio 里的分析助手。用户是钙钛矿/TCO 方向的研究者，\
正在处理原位吸收光谱与膜厚数据。

下面会给你「已知事实」——平台从数据库里查出来的真实数字。规则：

- 只根据已知事实回答。事实里没有的数字**不要编**，就说数据里没有。
- 事实里如果写了「已截断」或「没有逐个样品的明细」，就说明你看到的不是全部，
  不要下全局结论。
- 回答用中文，简洁，不要客套话，不要复述问题。
- 膜厚相关的判断要记住：光学厚度 OT = f/2，窗口越窄频率分辨率越差；
  条纹数不足时平台会打 LOW_CYCLES / DEGRADED 标志，那些数值不能当准确值用。
- 事实里的「曲线摘要」是批处理算出的曲线按时间等分、每段取平均后的结果（一行一条曲线，
  和「时间 (s)」那一行一一对应）。读膜厚时一定要对照「OT 可信比例」：比例低的那几段，
  膜厚数值不能当准确值用。摘要只够看趋势和形状，不要从里面读精确到个位的数。

如果用户的意图是**操作平台**而不是提问，在回答的最后附一个 json 代码块：

```json
{"action": "select", "filter": {...}, "why": "一句话说明"}
```

可用的 action：
- `narrow`：命中样品太多，你需要先缩小范围。filter 里给收窄条件。
- `select`：用户想挑一批样品。**只给筛选式，绝对不要给样品 ID 列表。**
  可用的键：batch（样品号数组）、folder（文件夹数组）、time（{from,to} ISO 时间）、
  name_prefix、q（模糊搜索）、has_matrix（布尔）。
- `process`：用户想跑批处理。给 filter 和 recipe（band_min/band_max 等）。

没有操作意图就不要附 json。附了也不会自动执行 —— 界面会渲染成一张卡片，
用户改过、点过才算数。所以宁可给一个明显偏保守的筛选式，也不要猜。
"""


GLOSSARY_PROMPT = """\
你是 HTE Studio 里的术语解释助手。用户在界面上点开了某个术语旁边的 ⓘ，\
想把这个概念弄明白。

下面会给你三样东西：

1. **平台里这条术语的权威定义** —— 这是界面上正在显示给用户看的原文。\
   你的回答必须和它一致。它没说的你可以补，它说了的不要改口，\
   更不要另编一套定义出来（用户会同时看到两份，对不上就没法用了）。
2. **用户眼前这一处的读数** —— 他点的那个 ⓘ 旁边正显示着的那几个数\
   （哪个时间窗、哪几个样品、各是多少、多少帧可信…）。\
   **他问的多半就是这几个数**，先看它再答，别只把定义换个说法重讲一遍。\
   这一段没给的时候，就说明他点的是个纯概念的 ⓘ，旁边本来就没有数据。
3. **用户当前库里的汇总统计** —— 用来把抽象定义落到他自己的数据上。

这三样里没有的数字，一个都不要编。要是这几样都答不上他的问题，\
就直说需要看什么才能答。

回答用中文，直接，不要客套话。用户问的是概念，不是要你操作平台 ——\
**不要输出 json 动作块。**
"""


def card_from(text: str) -> dict | None:
    """从回答里抠出动作卡片。抠不出来就没有卡片 —— 这是正常情况，不是错误。"""
    if "{" not in text:
        return None                       # 绝大多数回答是纯文字，别白跑一趟解析
    try:
        obj = extract_json(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ACTIONS or action == "none":
        return None
    card = {"action": action, "why": str(obj.get("why") or "")[:300]}

    flt = obj.get("filter")
    if isinstance(flt, dict):
        # 模型给了 ID 列表就丢掉那一部分。系统提示里已经说了不要给，
        # 但白名单要在代码里，不能只在提示词里 —— 提示词是建议，代码才是保证。
        flt = {k: v for k, v in flt.items() if k != "ids"}
        try:
            from app.storage import selection
            card["filter"] = selection.normalize(flt)
            card["count"] = selection.count(card["filter"])
        except (ValueError, TypeError) as exc:
            card["filter_error"] = f"模型给的筛选式不合法：{exc}"
            card["filter"] = flt
    if isinstance(obj.get("recipe"), dict):
        card["recipe"] = obj["recipe"]
    return card
