"""假的 OpenAI 兼容网关：只为浏览器验收用。

- 记录每个 /v1/chat/completions 请求体到 requests.jsonl（看模型到底收到了什么）
- 问题里有「收窄」→ 回一张 narrow 卡片
- 问题里有「慢」→ 40 段、每段 150 ms 慢慢流（用来测流式中途切换对话）
- 其余 → 一句短回答

智能体（请求里带 tools，或系统提示里有动作协议）按剧本走：
- 「有什么数据」→ 同时调 overview + list_fields → 拿到结果后回一句总结（带上读到的样品数）
- 「相关」→ 调 correlate(qa_x, qa_y) → 回一句带 r 的结论
- 「作废结果 123」→ 调 set_quality(reject) —— 要审批；批了之后回「已作废」，拒了回「好，没动」
- 「删除样品集 set_xxx」→ 调 delete_sample_set —— 每次都要审批
- 「一直查」→ 每轮都调 list_fields（撞轮数上限）
- 模型名是 fake-notools 时：带 tools 的请求一律回 400「tools is not supported」，逼平台退到动作协议
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(sys.argv[2])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            # 带一个同家族的旧版本（fake-1 → fake-2 是同一家族）—— 测速只测最新的那个
            body = json.dumps({"data": [{"id": "fake-1"}, {"id": "fake-2"}, {"id": "fake-notools"},
                                        {"id": "claude-fake"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
        if self.path.endswith("/v1/messages"):
            return self.anthropic(req)
        if req.get("max_tokens") == 8:                      # 设置页的测速探针
            return self.json(200, {"choices": [{"message": {"content": "ok"}}]})
        if req.get("tools") or "输出格式" in (req.get("messages") or [{}])[0].get("content", ""):
            return self.agent(req)
        q = next((m["content"] for m in reversed(req.get("messages", []))
                  if m.get("role") == "user"), "")
        # 只看问题本身。前面的「已知事实」里也可能出现「收窄」（样品多了，平台会提示范围太大）
        q = q.split("我的问题：")[-1]
        if "收窄" in q:
            parts = ["先收窄到 ZG0014 的两次测量。\n",
                     '```json\n{"action": "narrow", "filter": {"batch": ["ZG0014"]}, '
                     '"why": "只看 ZG0014"}\n```']
            delay = 0.02
        elif "慢" in q:
            parts = [f"第{i}段。" for i in range(40)]
            delay = 0.15
        else:
            parts = ["好的，", "收到。"]
            delay = 0.02

        if not req.get("stream"):
            body = json.dumps({"choices": [{"message": {"role": "assistant",
                                                        "content": "".join(parts)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for p in parts:
                chunk = {"choices": [{"delta": {"content": p}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(delay)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------------------------------------------------------- 智能体
    def agent(self, req):
        msgs = req.get("messages") or []
        action = "tools" not in req
        if req.get("model") == "fake-notools" and not action:
            return self.json(400, {"error": {"message": "tools is not supported by this model"}})
        # 动作协议下工具结果是一条「【工具结果 …】」的 user 消息
        last = msgs[-1] if msgs else {}
        got_result = last.get("role") == "tool" or (action and str(last.get("content", "")).startswith("【工具结果"))
        users = [m["content"] for m in msgs if m.get("role") == "user" and not str(m["content"]).startswith("【工具结果")]
        q = users[-1] if users else ""
        if got_result:
            results = []
            for m in reversed(msgs):
                c = str(m.get("content", ""))
                if m.get("role") == "tool" or c.startswith("【工具结果"):
                    results.append(c)
                else:
                    break
            text = " ".join(results)
            # ---- 文件、批处理、任务（P-49）
            if '"change_id"' in text:
                m = re.search(r'"path": "([^"]+)"', text)
                return self.reply(req, [], f"文件写好了：{m.group(1) if m else ''}。改之前备份过，可以撤销。", action)
            if '"encoding"' in text and '"lines"' in text:
                m = re.search(r'"lines": (\d+)', text)
                return self.reply(req, [], f"读到了，一共 {m.group(1)} 行。", action)
            if '"task_id"' in text and "get_task" in text and '"n_samples"' in text:
                m = re.search(r'"task_id": "([^"]+)"', text)
                return self.reply(req, [("get_task", {"task_id": m.group(1), "wait_seconds": 60})], "提交了，等它跑完。", action)
            if '"done": true' in text and '"run_id"' in text:
                m = re.search(r'"run_id": "([^"]+)"', text)
                return self.reply(req, [("batch_summary", {"run_id": m.group(1)})], "", action)
            if '"fields"' in text and '"n_ok"' in text:
                n = re.search(r'"n_ok": (\d+)', text).group(1)
                return self.reply(req, [], f"批处理跑完了：{n} 个样品成功。", action)
            if "一直查" in q:
                return self.reply(req, [("list_fields", {})], "", action)
            if "没有执行" in text:
                return self.reply(req, [], "好，没动。", action)
            if '"updated"' in text:
                return self.reply(req, [], "已作废，当前值退回了上一次。", action)
            if '"deleted"' in text:
                return self.reply(req, [], "样品集删掉了，样品还在。", action)
            m = re.search(r'"samples": (\d+)', text)
            if m:
                return self.reply(req, [], f"平台里有 **{m.group(1)} 个样品**。", action)
            m = re.search(r'"r": ([-0-9.]+)', text)
            if m:
                return self.reply(req, [], f"两者正相关，r = {float(m.group(1)):.3f}。", action)
            return self.reply(req, [], "查到了。", action)
        if m := re.search(r"写文件 (\S+)", q):
            return self.reply(req, [("write_file", {"path": m.group(1), "content": "# QA 报告\n\n膜厚偏薄。\n",
                                                    "note": "QA"})], "", action)
        if m := re.search(r"读文件 (\S+)", q):
            return self.reply(req, [("read_file", {"path": m.group(1)})], "", action)
        if m := re.search(r"跑批处理 (\S+)", q):
            return self.reply(req, [("run_batch", {"batch": m.group(1), "title": "QA 智能体批处理"})], "", action)
        if "有什么数据" in q:
            return self.reply(req, [("overview", {}), ("list_fields", {})], "我先看一下概况和字段。", action)
        if "相关" in q:
            return self.reply(req, [("correlate", {"x": "qa_x", "y": "qa_y"})], "", action)
        if m := re.search(r"作废结果 (\d+)", q):
            return self.reply(req, [("set_quality", {"result_ids": [int(m.group(1))], "quality": "reject",
                                                     "note": "QA 智能体"})], "", action)
        if m := re.search(r"删除样品集 (set_\w+)", q):
            return self.reply(req, [("delete_sample_set", {"set_id": m.group(1)})], "", action)
        if "一直查" in q:
            return self.reply(req, [("list_fields", {})], "", action)
        return self.reply(req, [], "你好，我可以帮你查平台里的数据。", action)

    def reply(self, req, calls, say, action):
        if action:
            text = json.dumps({"say": say, "calls": [{"name": n, "args": a} for n, a in calls]}, ensure_ascii=False)
            chunks = [{"content": text}]
        else:
            chunks = [{"content": say}] if say else []
            for i, (n, a) in enumerate(calls):
                arg = json.dumps(a, ensure_ascii=False)
                chunks.append({"tool_calls": [{"index": i, "id": f"call_{time.time_ns()}_{i}", "type": "function",
                                               "function": {"name": n, "arguments": arg[:4]}}]})
                chunks.append({"tool_calls": [{"index": i, "function": {"arguments": arg[4:]}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': c}]}, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"data: [DONE]\n\n")

    # ---------------------------------------------------------------- Anthropic 协议
    def anthropic(self, req):
        """/v1/messages：认 x-api-key、system 单独一个字段；流式按 content_block 事件发。"""
        if not self.headers.get("x-api-key") or self.headers.get("anthropic-version") != "2023-06-01":
            return self.json(401, {"type": "error", "error": {"message": "missing x-api-key / anthropic-version"}})
        if req.get("max_tokens") == 8:
            return self.json(200, {"content": [{"type": "text", "text": "ok"}]})
        sys_ok = "system" not in [m.get("role") for m in req.get("messages", [])]
        text = f"Anthropic 协议收到了（system 单列={sys_ok}）。"
        if not req.get("stream"):
            return self.json(200, {"content": [{"type": "text", "text": text}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        evs = [{"type": "message_start", "message": {}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
        evs += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ch}}
                for ch in (text[:8], text[8:])]
        evs += [{"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
        for e in evs:
            self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.03)

    def json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
