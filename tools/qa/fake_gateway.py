"""假的 OpenAI 兼容网关：只为浏览器验收用。

- 记录每个 /v1/chat/completions 请求体到 requests.jsonl（看模型到底收到了什么）
- 问题里有「收窄」→ 回一张 narrow 卡片
- 问题里有「慢」→ 40 段、每段 150 ms 慢慢流（用来测流式中途切换对话）
- 其余 → 一句短回答
"""
import json
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
            body = json.dumps({"data": [{"id": "fake-1"}]}).encode()
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


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
