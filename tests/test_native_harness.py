from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.agents.composition import ExecutionComposer, ModelCatalog, ModelOption
from app.agents.contracts import AgentAccess, AgentBudget, AgentPolicy, AgentRequest
from app.agents.harness_tools import ToolContext, execute_tool, tools_for
from app.agents.native_harness import (
    NativeHarnessProfile,
    NativeHarnessRuntime,
    resolve_api_key,
)
from app.agents.plan_graph import ModelBinding
from app.agents.profiles import load_agent_profiles
from app.agents.runtime_factory import default_model_catalog

_EXECUTION_RESULT = {
    "completed_steps": ["写 greeting"],
    "modified_files": ["out/greet.py"],
    "tests": [],
    "deviations": [],
    "remaining_risks": [],
    "next_steps": [],
}


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _response(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


class _ScriptedTransport:
    """Pops scripted API responses and records every request payload."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("scripted transport exhausted")
        return self.responses.pop(0)


def _request(
    workspace: Path,
    access: AgentAccess = AgentAccess.WORKSPACE_WRITE,
    policy: AgentPolicy | None = None,
    session_id: str = "",
    session_key: str = "executor",
    task_id: str = "TASK-native",
) -> AgentRequest:
    return AgentRequest(
        task_id=task_id,
        role="executor",
        instructions="完成示例任务并输出 ExecutionResult JSON。",
        workspace=workspace,
        access=access,
        policy=policy if policy is not None else AgentPolicy(network_allowed=True),
        budget=AgentBudget(total_timeout_seconds=20, idle_timeout_seconds=10),
        session_id=session_id,
        session_key=session_key,
    )


def _runtime(
    transport: _ScriptedTransport,
    workspace: Path,
    **overrides: Any,
) -> NativeHarnessRuntime:
    return NativeHarnessRuntime(
        NativeHarnessProfile(
            model="test-model",
            base_url="http://127.0.0.1:1/v1",
            api_key_env="WORKLOOP_NATIVE_TEST_KEY",
            transport=transport,
            **overrides,
        )
    )


class _EnvKey(unittest.TestCase):
    """Base class that provisions the fake API key env var."""

    def setUp(self) -> None:
        env = patch.dict(
            os.environ,
            {
                "WORKLOOP_NATIVE_TEST_KEY": "test-key",
                "WORKLOOP_NATIVE_BASE_URL": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)


class NativeHarnessRuntimeTest(_EnvKey):
    def test_tool_loop_writes_file_and_parses_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            (workspace / "hello.txt").write_text("hello workloop", encoding="utf-8")
            transport = _ScriptedTransport(
                [
                    _response(tool_calls=[_tool_call("c1", "read_file", {"path": "hello.txt"})]),
                    _response(
                        tool_calls=[
                            _tool_call(
                                "c2",
                                "write_file",
                                {"path": "out/greet.py", "content": "print('hi')"},
                            )
                        ]
                    ),
                    _response(content=json.dumps(_EXECUTION_RESULT, ensure_ascii=False)),
                ]
            )
            runtime = _runtime(transport, workspace)
            with patch.dict(os.environ, {"WORKLOOP_NATIVE_TEST_KEY": "test-key"}):
                result = runtime.invoke(_request(workspace))

            self.assertTrue(result.succeeded, result.error)
            self.assertEqual(result.output["modified_files"], ["out/greet.py"])
            self.assertEqual(
                (workspace / "out" / "greet.py").read_text(encoding="utf-8"),
                "print('hi')",
            )
            self.assertEqual(result.runtime, "native-harness")
            self.assertEqual(result.usage["input_tokens"], 30)
            self.assertEqual(result.usage["output_tokens"], 15)
            self.assertEqual(result.session_id, str(Path(result.session_id)))
            self.assertTrue(Path(result.session_id).is_file())

            event_types = [event.event_type.value for event in result.events]
            self.assertEqual(event_types.count("tool_started"), 2)
            self.assertEqual(event_types.count("tool_completed"), 2)
            self.assertIn("completed", event_types)

            first = transport.payloads[0]
            self.assertEqual(first["model"], "test-model")
            self.assertFalse(first["stream"])
            tool_names = {tool["function"]["name"] for tool in first["tools"]}
            self.assertIn("write_file", tool_names)
            self.assertIn("run_command", tool_names)
            system = first["messages"][0]["content"]
            self.assertIn("原生 Harness", system)
            second = transport.payloads[1]
            tool_results = [m for m in second["messages"] if m.get("role") == "tool"]
            self.assertIn("hello workloop", tool_results[0]["content"])

    def test_read_only_request_exposes_only_read_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport([_response(content='{"ok": true}')])
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(
                _request(workspace, access=AgentAccess.READ_ONLY, session_key="reviewer")
            )

            self.assertTrue(result.succeeded, result.error)
            tool_names = {tool["function"]["name"] for tool in transport.payloads[0]["tools"]}
            self.assertEqual(tool_names, {"read_file", "list_files", "search_content"})
            system = transport.payloads[0]["messages"][0]["content"]
            self.assertIn("只读", system)

    def test_unknown_tool_call_returns_error_feedback_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport(
                [
                    _response(
                        tool_calls=[_tool_call("c1", "delete_everything", {"force": True})]
                    ),
                    _response(content='{"ok": true}'),
                ]
            )
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(_request(workspace, access=AgentAccess.READ_ONLY))

            self.assertTrue(result.succeeded, result.error)
            second = transport.payloads[1]["messages"]
            tool_result = next(m for m in second if m.get("role") == "tool")
            self.assertIn("ERROR", tool_result["content"])
            self.assertIn("未知工具", tool_result["content"])

    def test_path_escape_and_protected_paths_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "ws"
            workspace.mkdir()
            policy = AgentPolicy(
                network_allowed=True,
                protected_paths=[".env", "secrets/**"],
            )
            transport = _ScriptedTransport(
                [
                    _response(
                        tool_calls=[
                            _tool_call("c1", "write_file", {"path": "../escape.txt", "content": "x"}),
                            _tool_call("c2", "write_file", {"path": ".env", "content": "x"}),
                            _tool_call("c3", "write_file", {"path": "secrets/key.pem", "content": "x"}),
                            _tool_call("c4", "read_file", {"path": ".env"}),
                        ]
                    ),
                    _response(content='{"ok": true}'),
                ]
            )
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(_request(workspace, policy=policy))

            self.assertTrue(result.succeeded, result.error)
            self.assertFalse((root / "escape.txt").exists())
            second = transport.payloads[1]["messages"]
            tool_results = [m["content"] for m in second if m.get("role") == "tool"]
            self.assertTrue(any("超出任务工作区" in text for text in tool_results))
            self.assertTrue(any("受项目策略保护" in text for text in tool_results))
            self.assertEqual(sum("受项目策略保护" in text for text in tool_results), 3)

    def test_shell_tool_gated_by_policy_and_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            deny = AgentPolicy(network_allowed=False)

            transport = _ScriptedTransport([_response(content='{"ok": true}')])
            runtime = _runtime(transport, workspace)
            with patch.dict(os.environ, {"WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR": ""}):
                result = runtime.invoke(_request(workspace, policy=deny))
            self.assertTrue(result.succeeded, result.error)
            tool_names = {tool["function"]["name"] for tool in transport.payloads[0]["tools"]}
            self.assertNotIn("run_command", tool_names)
            self.assertEqual(result.runtime_config["files_confined_to_worktree"], True)
            self.assertEqual(result.runtime_config["shell_tool"], "gated")

            transport_run = _ScriptedTransport(
                [
                    _response(
                        tool_calls=[
                            _tool_call("c1", "run_command", {"command": "echo native-harness-smoke"})
                        ]
                    ),
                    _response(content='{"ok": true}'),
                ]
            )
            runtime_run = _runtime(transport_run, workspace)
            with patch.dict(os.environ, {"WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR": "1"}):
                result_run = runtime_run.invoke(_request(workspace, policy=deny))
            self.assertTrue(result_run.succeeded, result_run.error)
            tool_names_run = {
                tool["function"]["name"] for tool in transport_run.payloads[0]["tools"]
            }
            self.assertIn("run_command", tool_names_run)
            tool_output = next(
                m["content"]
                for m in transport_run.payloads[1]["messages"]
                if m.get("role") == "tool"
            )
            self.assertIn("exit_code: 0", tool_output)
            self.assertIn("native-harness-smoke", tool_output)

    def test_session_persists_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            first_transport = _ScriptedTransport(
                [
                    _response(
                        tool_calls=[_tool_call("c1", "read_file", {"path": "hello.txt"})]
                    ),
                    _response(content=json.dumps(_EXECUTION_RESULT, ensure_ascii=False)),
                ]
            )
            runtime = _runtime(first_transport, workspace)
            (workspace / "hello.txt").write_text("resume me", encoding="utf-8")
            first = runtime.invoke(_request(workspace, task_id="TASK-resume"))
            self.assertTrue(first.succeeded, first.error)

            second_transport = _ScriptedTransport([_response(content='{"ok": true}')])
            runtime_second = _runtime(second_transport, workspace)
            second = runtime_second.invoke(
                _request(workspace, session_id=first.session_id, task_id="TASK-resume")
            )
            self.assertTrue(second.succeeded, second.error)
            resumed_messages = second_transport.payloads[0]["messages"]
            self.assertEqual(resumed_messages[0]["role"], "system")
            user_messages = [m for m in resumed_messages if m.get("role") == "user"]
            self.assertEqual(len(user_messages), 1)
            tool_messages = [m for m in resumed_messages if m.get("role") == "tool"]
            self.assertIn("resume me", tool_messages[0]["content"])

    def test_structured_output_failure_preserves_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport([_response(content="这不是 JSON")])
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(_request(workspace))

            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, "structured_output_failed")
            self.assertEqual(result.final_message, "这不是 JSON")
            self.assertEqual(result.events[-1].event_type.value, "failed")

    def test_truncated_output_reports_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport([_response(content='{"ok"', finish_reason="length")])
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(_request(workspace))

            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, "protocol_error")
            self.assertIn("max_tokens", result.error)

    def test_tool_round_limit_stops_endless_loops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            loop = _response(
                tool_calls=[_tool_call("c", "list_files", {})], finish_reason="tool_calls"
            )
            transport = _ScriptedTransport([loop, loop, loop, loop])
            runtime = _runtime(transport, workspace, max_tool_rounds=3)
            result = runtime.invoke(_request(workspace))

            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, "tool_round_limit")
            self.assertEqual(len(transport.payloads), 3)

    def test_cancel_interrupts_in_flight_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            release = threading.Event()

            def blocking_transport(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
                release.wait(timeout=30)
                return _response(content='{"ok": true}')

            runtime = NativeHarnessRuntime(
                NativeHarnessProfile(
                    model="test-model",
                    base_url="http://127.0.0.1:1/v1",
                    api_key_env="WORKLOOP_NATIVE_TEST_KEY",
                    transport=blocking_transport,
                )
            )
            outcome: dict[str, Any] = {}

            def run() -> None:
                outcome["result"] = runtime.invoke(
                    _request(workspace, task_id="TASK-cancel", session_key="executor")
                )

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            time.sleep(0.4)
            self.assertTrue(runtime.cancel("TASK-cancel"))
            thread.join(timeout=5)
            release.set()
            self.assertFalse(thread.is_alive())
            result = outcome["result"]
            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, "user_cancelled")
            self.assertEqual(result.events[-1].event_type.value, "cancelled")

    def test_cached_tokens_accumulate_into_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport(
                [
                    _response(
                        tool_calls=[_tool_call("c1", "list_files", {})],
                        prompt_tokens=100,
                        completion_tokens=7,
                        cached_tokens=40,
                    ),
                    _response(content='{"ok": true}'),
                ]
            )
            runtime = _runtime(transport, workspace)
            result = runtime.invoke(_request(workspace))

            self.assertTrue(result.succeeded, result.error)
            self.assertEqual(result.usage["input_tokens"], 110)
            self.assertEqual(result.usage["cached_input_tokens"], 40)
            self.assertEqual(result.usage["output_tokens"], 12)

    def test_health_check_reports_missing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            with patch.dict(os.environ, {"WORKLOOP_NATIVE_BASE_URL": ""}):
                missing_url = NativeHarnessRuntime(
                    NativeHarnessProfile(model="m", api_key_env="WORKLOOP_NATIVE_TEST_KEY")
                )
                self.assertFalse(missing_url.health_check()["available"])
                self.assertIn("base_url", missing_url.health_check()["error"])

                runtime = _runtime(_ScriptedTransport([]), workspace)
                with patch.dict(os.environ, {"WORKLOOP_NATIVE_TEST_KEY": ""}):
                    health = runtime.health_check()
                self.assertFalse(health["available"])
                self.assertIn("API key", health["error"])

                with patch.dict(os.environ, {"WORKLOOP_NATIVE_TEST_KEY": "k"}):
                    self.assertTrue(runtime.health_check()["available"])

    def test_missing_credentials_fail_before_any_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            transport = _ScriptedTransport([])
            runtime = _runtime(transport, workspace)
            with patch.dict(os.environ, {"WORKLOOP_NATIVE_TEST_KEY": ""}):
                result = runtime.invoke(_request(workspace))
            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, "environment_missing")
            self.assertEqual(transport.payloads, [])


class HarnessToolLayerTest(unittest.TestCase):
    def test_edit_file_requires_unique_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            target = workspace / "code.txt"
            target.write_text("a\nb\na\n", encoding="utf-8")
            ctx = ToolContext(
                workspace=workspace,
                policy=AgentPolicy(network_allowed=True),
                access=AgentAccess.WORKSPACE_WRITE,
            )
            tools = tools_for(ctx.access, ctx.policy, shell_allowed=True)

            ambiguous = execute_tool(
                tools, "edit_file", json.dumps({"path": "code.txt", "old_string": "a", "new_string": "z"}), ctx
            )
            self.assertIn("ERROR", ambiguous)
            self.assertIn("2 次", ambiguous)

            unique = execute_tool(
                tools,
                "edit_file",
                json.dumps({"path": "code.txt", "old_string": "b", "new_string": "y"}),
                ctx,
            )
            self.assertNotIn("ERROR", unique)
            self.assertEqual(target.read_text(encoding="utf-8"), "a\ny\na\n")

    def test_search_content_matches_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "a.py").write_text("alpha\nbeta\n", encoding="utf-8")
            (workspace / "sub").mkdir()
            (workspace / "sub" / "b.py").write_text("gamma\n", encoding="utf-8")
            ctx = ToolContext(
                workspace=workspace,
                policy=AgentPolicy(network_allowed=True),
                access=AgentAccess.READ_ONLY,
            )
            tools = tools_for(ctx.access, ctx.policy, shell_allowed=False)
            result = execute_tool(
                tools, "search_content", json.dumps({"pattern": "alpha|gamma"}), ctx
            )
            self.assertIn("a.py:1: alpha", result)
            self.assertIn("sub/b.py:1: gamma", result)

    def test_absolute_path_inside_workspace_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            ctx = ToolContext(
                workspace=workspace,
                policy=AgentPolicy(network_allowed=True),
                access=AgentAccess.WORKSPACE_WRITE,
            )
            tools = tools_for(ctx.access, ctx.policy, shell_allowed=False)
            result = execute_tool(
                tools,
                "write_file",
                json.dumps({"path": str(workspace / "abs.txt"), "content": "ok"}),
                ctx,
            )
            self.assertNotIn("ERROR", result)
            self.assertEqual((workspace / "abs.txt").read_text(encoding="utf-8"), "ok")


class NativeCatalogTest(unittest.TestCase):
    def test_model_option_round_trips_native_fields(self) -> None:
        option = ModelOption(
            profile_id="deepseek-writer",
            label="DeepSeek writer",
            runtime="native",
            provider="deepseek",
            model="DeepSeek-V4-Flash",
            access=AgentAccess.WORKSPACE_WRITE,
            capabilities=["implementation", "general"],
            quality=4,
            input_cost_per_million=0.28,
            output_cost_per_million=0.42,
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
            max_tokens=8192,
        )
        self.assertEqual(option.runtime, "native")
        restored = ModelOption.from_dict(option.to_dict())
        self.assertEqual(restored.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(restored.api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(restored.max_tokens, 8192)

    def test_invalid_base_url_and_env_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ModelOption.from_dict(
                {
                    "profile_id": "x",
                    "label": "X",
                    "runtime": "native",
                    "model": "m",
                    "access": "workspace_write",
                    "capabilities": ["implementation"],
                    "base_url": "ftp://nope",
                }
            )
        with self.assertRaises(ValueError):
            ModelOption.from_dict(
                {
                    "profile_id": "x",
                    "label": "X",
                    "runtime": "native",
                    "model": "m",
                    "access": "workspace_write",
                    "capabilities": ["implementation"],
                    "api_key_env": "not a name",
                }
            )

    def test_model_binding_accepts_native_runtime(self) -> None:
        ModelBinding(profile_id="p", runtime="native", model="m").validate()
        with self.assertRaises(ValueError):
            ModelBinding(profile_id="p", runtime="bogus", model="m").validate()

    def test_composer_routes_native_profiles(self) -> None:
        catalog = ModelCatalog(
            [
                ModelOption(
                    profile_id="planner",
                    label="Planner",
                    runtime="native",
                    model="m-plan",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning", "general"],
                    quality=4,
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                ),
                ModelOption(
                    profile_id="writer",
                    label="Writer",
                    runtime="native",
                    model="m-write",
                    access=AgentAccess.WORKSPACE_WRITE,
                    capabilities=["implementation", "frontend", "backend", "general"],
                    quality=4,
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                ),
                ModelOption(
                    profile_id="reviewer",
                    label="Reviewer",
                    runtime="native",
                    model="m-review",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["review", "general"],
                    quality=4,
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                ),
            ]
        )
        plan = type(
            "Plan",
            (),
            {
                "requirement_understanding": "做一个前端页面",
                "steps": ["实现页面结构", "实现接口对接"],
            },
        )()
        graph = ExecutionComposer(catalog).compose(plan)
        self.assertEqual(graph.planning_model.runtime, "native")
        self.assertEqual(graph.review_model.runtime, "native")
        self.assertTrue(all(node.model.runtime == "native" for node in graph.nodes))
        self.assertEqual(graph.nodes[0].model.model, "m-write")

    def test_role_profiles_accept_native_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent-profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "roles": {
                            "planner": {"runtime": "native", "model": "m", "access": "read_only"},
                            "executor": {"runtime": "native", "model": "m", "access": "workspace_write"},
                            "reviewer": {"runtime": "native", "model": "m", "access": "read_only"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            profiles = load_agent_profiles(path)
            self.assertEqual(profiles["executor"].runtime, "native")


class NativeApiKeyResolutionTest(unittest.TestCase):
    def test_env_var_takes_priority_over_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key_file = Path(temporary) / "key.txt"
            key_file.write_text("file-key", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"WORKLOOP_NATIVE_API_KEY": "", "WORKLOOP_NATIVE_KEY_FILE": str(key_file)},
            ):
                self.assertEqual(resolve_api_key("WORKLOOP_NATIVE_API_KEY"), "file-key")
            with patch.dict(
                os.environ,
                {"WORKLOOP_NATIVE_API_KEY": "env-key", "WORKLOOP_NATIVE_KEY_FILE": str(key_file)},
            ):
                self.assertEqual(resolve_api_key("WORKLOOP_NATIVE_API_KEY"), "env-key")

    def test_key_file_accepts_key_value_and_json_formats(self) -> None:
        from app.agents.native_harness import _read_key_file

        with tempfile.TemporaryDirectory() as temporary:
            kv = Path(temporary) / "kv.txt"
            kv.write_text("# comment\nDEEPSEEK_API_KEY=abc123def456ghi789\n", encoding="utf-8")
            self.assertEqual(_read_key_file(str(kv)), "abc123def456ghi789")
            structured = Path(temporary) / "json.txt"
            structured.write_text('{"apiKey": "sk-json-key-123456"}', encoding="utf-8")
            self.assertEqual(_read_key_file(str(structured)), "sk-json-key-123456")


class NativeServerWiringTest(unittest.TestCase):
    def test_native_env_yields_cli_free_default_catalog(self) -> None:
        from app.web.server import make_server

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {
                "WORKLOOP_NATIVE_BASE_URL": "https://api.deepseek.com/v1",
                "WORKLOOP_NATIVE_MODEL": "DeepSeek-V4-Flash",
                "WORKLOOP_NATIVE_API_KEY_ENV": "DEEPSEEK_API_KEY",
                "WORKLOOP_NATIVE_EXECUTOR_MODEL": "DeepSeek-V4",
            }
            with patch.dict(os.environ, env):
                catalog = default_model_catalog()
                models = {m.profile_id: m for m in catalog.list_all()}
                self.assertTrue(all(m.runtime == "native" for m in models.values()))
                self.assertEqual(models["executor"].model, "DeepSeek-V4")
                self.assertEqual(models["executor"].api_key_env, "DEEPSEEK_API_KEY")

                server = make_server(root, 0, auto_run_agent=False)
                try:
                    routed = server.agent_workflow.runtime
                    self.assertTrue(
                        all(
                            isinstance(runtime, NativeHarnessRuntime)
                            for runtime in routed.profile_runtimes.values()
                        )
                    )
                    health = routed.profile_runtimes["executor"].health_check()
                    # No key in this test environment: health explains exactly
                    # what is missing instead of pretending to be available.
                    self.assertFalse(health["available"])
                    self.assertIn("DEEPSEEK_API_KEY", health["error"])
                finally:
                    server.server_close()


class NativeHttpTransportTest(unittest.TestCase):
    def test_urllib_transport_posts_and_parses(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from app.agents.native_harness import urllib_transport

        seen: dict[str, Any] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen["auth"] = self.headers.get("Authorization")
                seen["path"] = self.path
                seen["body"] = json.loads(body)
                if seen["body"].get("model") == "boom":
                    payload = b'{"error": "internal"}'
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps(_response(content='{"ok": true}')).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}/v1"
            transport = urllib_transport(base, "secret-key")
            response = transport({"model": "m", "messages": []}, timeout=10)
            self.assertEqual(response["choices"][0]["message"]["content"], '{"ok": true}')
            self.assertEqual(seen["auth"], "Bearer secret-key")
            self.assertEqual(seen["path"], "/v1/chat/completions")
            with self.assertRaises(RuntimeError) as context:
                transport({"model": "boom", "messages": []}, timeout=10)
            self.assertIn("HTTP 500", str(context.exception))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
