# 浏览器验收脚本

改完前端（尤其是页面的异步逻辑、AI 抽屉）跑一遍。每条检查都输出一个量出来的数，
不是「看起来没问题」。这些脚本在 2026-11-06 的 A2 修复里先对**旧前端**跑过、12 条全部失败，
再对新前端跑、全部通过 —— 说明它们确实抓得住那些问题。

## 准备（一次）

```bash
# 1. 生成样例数据
.venv/bin/python sample_data/make_insitu.py

# 2. 起一个假的模型网关（AI 那两条检查要用；会把模型收到的请求记进 requests.jsonl）
python3 tools/qa/fake_gateway.py 8797 /tmp/requests.jsonl &

# 3. 在一个干净的工作区起平台（端口 8799）
HTE_WORKSPACE=/tmp/hte-qa HTE_NO_BROWSER=1 HTE_PORT=8799 .venv/bin/python -m app.main &
```

然后在界面上（或用接口）：导入 `sample_data/insitu/`、跑一次批处理、
在「设置 → 模型」里把接口地址填成 `http://127.0.0.1:8797/v1`，密钥随便填、模型名 `fake-1`。

## 跑

```bash
# 参数：一个样品 id、一次批处理的 run id
NODE_PATH=$(npm root -g) node tools/qa/a2_check.js smp_xxx run_xxx
NODE_PATH=$(npm root -g) node tools/qa/a2_download_name.js smp_xxx
# AI 曲线摘要（P-44）：看假网关记下的、模型实际收到的文字里有没有曲线摘要
NODE_PATH=$(npm root -g) node tools/qa/p44_check.js smp_xxx /tmp/requests.jsonl
```

需要 Playwright（`npm i -g playwright`，或已经装好的）。

## 检查了什么

| 编号 | 检查 | 判据 |
|---|---|---|
| #4 | 点一次导航渲染几遍 | `/selection/query` 只调 1 次 |
| #5 | 进出数据处理页 4 次后拖一个文件 | 只上传 1 遍 |
| #6 | 跑着批处理离开对比页 | 5 秒后标题仍是「总览」 |
| #18 | 搜索请求还在路上时清空搜索框 | 结果区保持空 |
| #19 | 拖数字滑块 20 帧再松手 | 计算请求 ≤ 2 次 |
| #20 | 样品页下载的文件名 | 以样品名开头 |
| #21 | 打开 `#/settings` | 进设置页 |
| #22 | 曲线加载失败后点「重试」 | 错误消失 |
| #27 | 对比页参数区说明 | 没有原样的 `**` |
| #7 | AI「用这个范围继续问」之后的下一问 | 带的是收窄的范围 |
| #23 | AI 流式回答途中开新对话 | 旧回答不写进新对话，按钮回到「发送」 |
| — | 设置页存完马上离开 | 不被拽回设置页 |

编号对应 `docs/plan/index.html` §6.2。
