"""End-to-end smoke for the native harness runtime against a real API.

Reads the key like configure_pi_provider.py does (env var first, then the key
file), resolves the served model id from {base_url}/models, then runs one
real task through NativeHarnessRuntime: read hello.txt, write greeting.txt,
return an ExecutionResult JSON object.

Usage:
    D:\\python\\python.exe e2e_native.py [--model DeepSeek-V4-Flash]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from configure_pi_provider import DEFAULT_KEY_FILE, probe_models, read_key

from app.agents.contracts import AgentAccess, AgentBudget, AgentPolicy, AgentRequest
from app.agents.native_harness import NativeHarnessProfile, NativeHarnessRuntime

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    key = read_key(DEFAULT_KEY_FILE)
    print("endpoint:", args.base_url)
    model_ids = [
        str(item.get("id"))
        for item in probe_models(args.base_url, key)
        if isinstance(item, dict) and item.get("id")
    ]
    print("served models:", ", ".join(model_ids))
    lowered = {item.lower(): item for item in model_ids}
    squashed = re.sub(r"[^a-z0-9]", "", args.model.lower())
    model = next(
        (
            lowered[key_]
            for key_ in lowered
            if key_.lower() == args.model.lower()
            or re.sub(r"[^a-z0-9]", "", key_) == squashed
        ),
        args.model,
    )
    print("model:", model)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary) / "ws"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("hello native harness\n", encoding="utf-8")

        runtime = NativeHarnessRuntime(
            NativeHarnessProfile(
                model=model,
                base_url=args.base_url,
                api_key_env="DEEPSEEK_API_KEY",
                max_tokens=4096,
            )
        )
        request = AgentRequest(
            task_id="TASK-native-smoke",
            role="executor",
            instructions=(
                "读取工作区里的 hello.txt，把它的内容转成大写后写入 greeting.txt，"
                "然后按 ExecutionResult Schema 输出 JSON。"
            ),
            workspace=workspace,
            access=AgentAccess.WORKSPACE_WRITE,
            policy=AgentPolicy(network_allowed=False),
            budget=AgentBudget(total_timeout_seconds=300, idle_timeout_seconds=120),
            session_key="executor",
        )
        print("runtime config:", json.dumps(runtime.describe(request)["config"], ensure_ascii=False))
        result = runtime.invoke(request)

        print("succeeded:", result.succeeded)
        print("error:", result.error or "-")
        print("usage:", result.usage)
        greeting = workspace / "greeting.txt"
        if greeting.is_file():
            print("greeting.txt:", greeting.read_text(encoding="utf-8").strip())
        else:
            print("greeting.txt: MISSING")
        tool_events = [
            event.data.get("name")
            for event in result.events
            if event.event_type.value == "tool_completed"
        ]
        print("tool calls:", tool_events)
        print("output:", json.dumps(result.output, ensure_ascii=False)[:400])
        return 0 if result.succeeded and greeting.is_file() else 1


if __name__ == "__main__":
    sys.exit(main())
