"""Minimal OpenAI-compatible endpoint for driving Workloop V2 locally.

Serves ``POST /v1/chat/completions`` with direct structured JSON responses for
the built-in Workloop node contracts. Implementation responses use the V2
``file_changes`` field, so the endpoint exercises the same atomic workspace
publish path as a real model. Use it to demo or develop the native runtime
without a paid API key:

    python demo/mock_openai_server.py --port 8977

然后在控制台「接入模型」里填 Base URL http://127.0.0.1:8977/v1，
模型名任意（例如 mock-model），API Key 任意非空字符串。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELLO_CONTENT = (
    "def greet(name):\n"
    "    return f\"你好，{name}！\"\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    print(greet(\"世界\"))\n"
)


def _completion(content: str) -> dict:
    message: dict = {"role": "assistant", "content": content}
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def respond(messages: list[dict]) -> dict:
    """Pick a scripted reply from the node type in the V2 JSON request."""
    payload = _request_payload(messages)
    node_type = str(payload.get("node_type", ""))
    context = payload.get("shared_context", {})
    inputs = context.get("inputs", {}) if isinstance(context, dict) else {}
    project = inputs.get("project", {}) if isinstance(inputs, dict) else {}
    commands = project.get("validation_commands", []) if isinstance(project, dict) else []

    if node_type == "requirement":
        return _completion(json.dumps({
            "understanding": "在项目中新增 hello.py，提供 greet(name) 问候函数（mock 需求）。",
            "acceptance_criteria": [
                "hello.py 定义了 greet 函数",
                "greet('世界') 返回包含『世界』的问候文本",
            ],
            "open_questions": [],
        }, ensure_ascii=False))

    if node_type == "planning":
        required_tests = [list(command) for command in commands[:1]] if isinstance(commands, list) else []
        return _completion(json.dumps({
            "steps": ["创建 hello.py，实现 greet(name) 并附最小可运行入口"],
            "risks": [],
            "artifacts": {"required_tests": required_tests},
        }, ensure_ascii=False))

    if node_type == "implementation":
        return _completion(json.dumps({
            "changes": "创建 hello.py 并实现 greet(name)。",
            "file_changes": [{"operation": "write", "path": "hello.py", "content": HELLO_CONTENT}],
            "artifacts": {},
            "decisions": [],
        }, ensure_ascii=False))

    if node_type == "testing":
        return _completion(json.dumps({
            "checks": [],
            "risks": [],
            "decisions": [],
        }, ensure_ascii=False))

    if node_type == "review":
        return _completion(json.dumps({
            "verdict": "pass",
            "issues": [],
            "decisions": ["mock 审核通过：实现与计划一致。"],
        }, ensure_ascii=False))

    if node_type == "tool":
        return _completion(json.dumps({"result": "ok"}, ensure_ascii=False))

    return _completion(json.dumps({"result": "ok"}, ensure_ascii=False))


def _parse_json(content: str) -> object:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _request_payload(messages: list[dict]) -> dict:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        parsed = _parse_json(str(message.get("content") or ""))
        if isinstance(parsed, dict):
            return parsed
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send(404, {"error": f"unknown path {self.path}"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages must be an array")
            self._send(200, respond(messages))
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"error": str(error)})

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8977)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"mock OpenAI endpoint listening on http://{args.host}:{args.port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
