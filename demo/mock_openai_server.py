"""Minimal OpenAI-compatible endpoint for driving the native harness locally.

Serves ``POST /v1/chat/completions`` with scripted responses for the three
Workloop roles (planner / executor / reviewer), including a real write_file
tool-call round so the in-process tool loop is exercised end to end. Use it
to demo or develop the native runtime without a paid API key:

    py demo/mock_openai_server.py --port 8977

然后在控制台「接入模型」里填 Base URL http://127.0.0.1:8977/v1，
模型名任意（例如 mock-model），API Key 任意非空字符串。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_CRITERIA_PATTERN = re.compile(r"\"acceptance_criteria\"\s*:\s*\[(.*?)\]", re.S)
_COMMANDS_PATTERN = re.compile(r"\"available_validation_commands\"\s*:\s*\[(.*?)\]", re.S)

HELLO_CONTENT = (
    "def greet(name):\n"
    "    return f\"你好，{name}！\"\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    print(greet(\"世界\"))\n"
)


def _string_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item for item in re.findall(r"\"((?:[^\"\\\\]|\\\\.)*)\"", raw)]


def _completion(content: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def respond(messages: list[dict]) -> dict:
    """Pick a scripted reply from the role named in the prompt."""
    text = "\n".join(
        str(message.get("content") or "") for message in messages if isinstance(message, dict)
    )
    ran_tools = any(message.get("role") == "tool" for message in messages)

    if "ExecutionPlan" in text:
        commands = _string_list(_COMMANDS_PATTERN.search(text).group(1) if _COMMANDS_PATTERN.search(text) else None)
        plan = {
            "requirement_understanding": "在项目中新增 hello.py，提供 greet(name) 问候函数（mock 计划）。",
            "non_goals": [],
            "files_and_symbols": ["hello.py: greet"],
            "steps": ["创建 hello.py，实现 greet(name) 并附最小可运行入口"],
            "constraints": [],
            "acceptance_criteria": [
                "hello.py 定义了 greet 函数",
                "greet('世界') 返回包含『世界』的问候文本",
            ],
            "required_tests": commands[:1] or ["workloop-check"],
            "risks": [],
            "open_questions": [],
        }
        return _completion(json.dumps(plan, ensure_ascii=False))

    if "ExecutionResult" in text:
        if ran_tools:
            result = {
                "completed_steps": ["创建 hello.py 并实现 greet"],
                "modified_files": ["hello.py"],
                "tests": [],
                "deviations": [],
                "remaining_risks": [],
                "next_steps": [],
            }
            return _completion(json.dumps(result, ensure_ascii=False))
        return _completion(
            "",
            tool_calls=[
                _tool_call("call-write", "write_file", {"path": "hello.py", "content": HELLO_CONTENT})
            ],
        )

    if "ReviewResult" in text or "verdict" in text:
        match = _CRITERIA_PATTERN.search(text)
        criteria = _string_list(match.group(1) if match else None) or ["mock 验收"]
        review = {
            "verdict": "pass",
            "acceptance": [{"criterion": criterion, "passed": True} for criterion in criteria],
            "issues": [],
            "recommended_tests": [],
            "summary": "mock 审核通过：实现与计划一致。",
        }
        return _completion(json.dumps(review, ensure_ascii=False))

    return _completion("ok")


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
