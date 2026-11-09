# AGENTS.md —— 给在这个仓库里工作的任何编码代理 / 模型

这一份和 `CLAUDE.md` 是同一套规则（那份是给 Claude 的，这份给其他模型和工具，例如 DeepSeek、Codex、Cursor……）。
**两份冲突时以 `CLAUDE.md` 为准**；改规则时两份一起改。

## 先读什么
1. **`docs/HANDOFF.md`** —— 交接文档：这个平台是什么、十条设计思想、术语、每个菜单的逻辑、代码地图、
   九种常见改动各改哪里、测试与验收、记录体系、踩过的坑、现状与下一步、开工清单
2. **`docs/REFERENCE.md`** —— 从代码生成的参考手册：全部接口、页面、后台任务、智能体工具、数据表、环境变量、QFLS 阈值
3. `docs/plan/index.html` —— 项目计划、用户明确指定的（P-##）和自行决定的（D-##）两张登记表、「等你定」

## 硬规则（摘要；全文见 CLAUDE.md 和 HANDOFF.md §0）
- **每一次提交都写一份修改报告** `docs/plan/changes/C###_日期_本提交.md`，和代码在同一个提交里；
  分清「用户明确指定的」(P-##) 和「我自行决定的」(D-##)，新登记项同步进 `docs/plan/index.html` §3 / §4、附录加一行
- 不是用户给的数值、默认值、阈值、算法口径，一律登记为 D 项；影响数值或科学结论的标「建议你确认」并在回复里点名；
  拿不准用户是不是这个意思，标「推断」，不要写成「你要求的」
- `app/analysis/fringe_ot.py` 的 `DEFAULTS` 是冻结的规范原文，**不许改**（用 override）
- **原始数据永远只读**
- 测试：`.venv/bin/python -m pytest -q`；静态检查：`.venv/bin/ruff check app tests tools`（目标 Python 3.11）；
  改了前端用 Playwright 把受影响的页面打开看 console（`tools/qa/`）
- 改了接口 / 页面 / 智能体工具 / 表 / 环境变量 / QFLS 阈值 → `.venv/bin/python tools/gen_reference.py`
- 提交信息、报告里不写模型名称
- 路由一律普通 `def`；前端遵守页面生命周期（`docs/HANDOFF.md` §6.3）
- 用户的界面语言是中文：界面文字、报错、报告都用中文，说人话
