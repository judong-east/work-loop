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
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

from app.agents.contracts import AgentAccess, AgentBudget, AgentPolicy, AgentRequest
from app.agents.native_harness import NativeHarnessProfile, NativeHarnessRuntime

DEFAULT_KEY_FILE = Path(r"C:\Users\23393\Desktop\密钥.txt")
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


def read_key() -> str:
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    raw = DEFAULT_KEY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for name in ("apiKey", "api_key", "key", "token", "DEEPSEEK_API_KEY"):
                if isinstance(data.get(name), str) and data[name].strip():
                    return data[name].strip()
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("sk-"):
            line = line.split("=", 1)[1]
        return line.strip().strip("\"'")
    raise SystemExit("FATAL: no key found")


def probe_models(base_url: str, key: str) -> list[str]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    key = read_key()
    print("endpoint:", args.base_url)
    model_ids = probe_models(args.base_url, key)
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
