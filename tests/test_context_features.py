from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.application.model_invocation import ModelInvocationService
from app.application.workbench import WorkbenchService
from app.domain.context_compaction import ContextCompactor, estimate_tokens
from app.domain.models import (
    ContextState,
    ModelAlias,
    ModelProvider,
    Session,
    SessionMode,
    WorkflowNode,
    default_runtime_policy,
)
from app.domain.tooling import SEARCH_TOOLS
from app.infrastructure.model_gateway import OpenAICompatibleGateway
from app.infrastructure.resource_center import ResourceCenter
from app.infrastructure.zvec_grep import ZvecGrepClient, ZvecGrepConfig


class ContextCompactionTest(unittest.TestCase):
    def _session(self) -> Session:
        return Session.create("project", "large context", SessionMode.TASK)

    def _node(self) -> WorkflowNode:
        return WorkflowNode(
            "plan",
            "planning",
            config={
                "compaction": {
                    "context_window_tokens": 1_000,
                    "reserve_tokens": 100,
                    "keep_recent_tokens": 100,
                    "summary_max_tokens": 100,
                    "max_compactions": 1,
                }
            },
        )

    def _context(self) -> ContextState:
        return ContextState(
            facts={"large_fact": "事实 " * 5_000},
            artifacts={"report.md": "报告 " * 3_000},
            decisions=[f"decision-{index}" for index in range(30)],
            inputs={
                "request": "request " * 2_000,
                "project": {
                    "project_id": "project",
                    "name": "large",
                    "instructions": "instruction " * 5_000,
                    "runtime_policy": {},
                },
                "workspace": {"files": []},
            },
        )

    def test_compaction_is_bounded_and_does_not_mutate_authoritative_context(self):
        session = self._session()
        context = self._context()
        original = context.to_dict()
        summaries: list[tuple[str, dict | None]] = []

        def summarize(value: str, previous: dict | None) -> dict:
            summaries.append((value, previous))
            return {"goal": "keep this goal", "decisions": ["verified decision"]}

        view = ContextCompactor(summary_callback=summarize).prepare(
            session=session,
            node=self._node(),
            context=context,
        )
        self.assertTrue(view.compacted)
        self.assertLessEqual(view.estimated_tokens, 900)
        self.assertEqual(context.to_dict(), original)
        self.assertTrue(summaries)
        compaction_events = [
            message for message in session.messages
            if message.metadata.get("context_compaction")
        ]
        self.assertEqual(len(compaction_events), 1)
        entry = compaction_events[0].metadata["context_compaction"]
        self.assertGreater(entry["tokens_before"], entry["tokens_after"])

    def test_compaction_quota_does_not_append_duplicate_events(self):
        session = self._session()
        context = self._context()
        compactor = ContextCompactor()
        first = compactor.prepare(session=session, node=self._node(), context=context)
        second = compactor.prepare(session=session, node=self._node(), context=context)
        self.assertTrue(first.compacted)
        self.assertTrue(second.compacted)
        self.assertEqual(
            sum(1 for message in session.messages if message.metadata.get("context_compaction")),
            1,
        )

    def test_zero_compaction_quota_only_returns_a_bounded_view(self):
        session = self._session()
        node = WorkflowNode(
            "plan",
            "planning",
            config={
                "compaction": {
                    "context_window_tokens": 1_000,
                    "reserve_tokens": 100,
                    "keep_recent_tokens": 100,
                    "max_compactions": 0,
                }
            },
        )
        view = ContextCompactor().prepare(session=session, node=node, context=self._context())
        self.assertTrue(view.compacted)
        self.assertFalse(view.compaction_id)
        self.assertFalse(any(message.metadata.get("context_compaction") for message in session.messages))

    def test_default_policy_stays_usable_on_a_small_window_model(self):
        """An 8k model must not be vetoed by the default keep-recent value."""

        budget = ContextCompactor().budget_for(
            context=ContextState(inputs={"project": {"runtime_policy": default_runtime_policy()}}),
            node=WorkflowNode("plan", "planning"),
            model_context_window=8_192,
        )
        self.assertEqual(budget.context_window_tokens, 8_192)
        self.assertLess(budget.recent_tokens, budget.trigger_tokens)
        self.assertGreater(budget.recent_tokens, 0)

    def test_keep_recent_tokens_bounds_recent_history(self):
        """The configured recent budget must change what is actually retained."""

        def pack_for(keep_recent: int) -> dict:
            session = Session.create("project", "history", SessionMode.TASK)
            for index in range(40):
                session.add_message(
                    "tool",
                    f"tool output {index} " + "x" * 400,
                    node_id="plan",
                    metadata={"tool_event": {"name": "zvec_grep_rg"}},
                )
            node = WorkflowNode(
                "plan",
                "planning",
                config={"compaction": {
                    "context_window_tokens": 200_000,
                    "keep_recent_tokens": keep_recent,
                }},
            )
            view = ContextCompactor().prepare(
                session=session,
                node=node,
                context=ContextState(inputs={"request": "go"}),
            )
            return view.context_pack

        small = pack_for(200)
        large = pack_for(20_000)
        self.assertLess(
            len(small["recent_tools"]),
            len(large["recent_tools"]),
            "keep_recent_tokens must govern retained recent history",
        )
        self.assertLessEqual(estimate_tokens(small["recent_tools"]), 400)


class ZvecGrepAdapterTest(unittest.TestCase):
    def test_semantic_search_uses_local_refresh_off_and_preserves_freshness(self):
        client = ZvecGrepClient(ZvecGrepConfig(command=("zg",)))
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.CompletedProcess(
                ["zg"], 0, stdout="fresh result", stderr=""
            )
            with mock.patch.object(client, "_run", return_value=completed) as runner:
                result = client.semantic_search(
                    root=Path(tmp).resolve(),
                    query="context compaction",
                    freshness="eventual",
                )
            argv = runner.call_args.args[0]
            self.assertIn("--refresh", argv)
            self.assertEqual(argv[argv.index("--refresh") + 1], "off")
            self.assertEqual(result["freshness"], "eventual")
            self.assertEqual(result["route"], "semantic")

    def test_exact_search_passes_managed_rg_arguments_without_shell(self):
        client = ZvecGrepClient()
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.CompletedProcess(["zg"], 0, stdout="src/a.py:1:needle", stderr="")
            with mock.patch.object(client, "_run", return_value=completed) as runner:
                result = client.exact_search(
                    root=Path(tmp).resolve(),
                    pattern="needle",
                    path="src",
                    literal=True,
                    context_lines=2,
                )
            argv = runner.call_args.args[0]
            self.assertEqual(argv[0], "zg")
            self.assertEqual(argv[1:6], ["query", "--preview", "short", "--mode", "direct"])
            self.assertEqual(argv[6], "--rg")
            self.assertIn("-F", argv)
            self.assertIn("-C", argv)
            self.assertNotIn("|", argv)
            self.assertEqual(result["route"], "exact")


class GatewayToolLoopTest(unittest.TestCase):
    def test_openai_tool_loop_executes_local_tool_and_returns_final_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider("local", "Local", "http://127.0.0.1:1234/v1", auth_type="none"))
            resources.save_model(ModelAlias("model", "local", "model-1", protocol="openai"))
            gateway = OpenAICompatibleGateway(resources)
            responses = [
                {
                    "choices": [{"message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "zvec_grep_rg",
                                "arguments": '{"pattern":"needle","literal":true}',
                            },
                        }],
                    }}]
                },
                {"choices": [{"message": {"content": '{"result":"done"}'}}]},
            ]
            executor_calls: list[tuple[str, dict]] = []

            def execute(name: str, arguments: dict) -> dict:
                executor_calls.append((name, arguments))
                return {"output": "src/a.py:1:needle"}

            with mock.patch.object(gateway, "_request_json", side_effect=responses) as request:
                output, events = gateway.complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool"),
                    context=ContextState(inputs={"project": {"default_model": "model"}}),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    tool_executor=execute,
                )
            self.assertEqual(output["result"], "done")
            self.assertEqual(executor_calls, [("zvec_grep_rg", {"pattern": "needle", "literal": True})])
            self.assertEqual(events[0]["name"], "zvec_grep_rg")
            self.assertEqual(request.call_count, 2)

    def test_tool_loop_keeps_every_request_within_the_transcript_budget(self):
        """The budget must hold across rounds, not only before the first one."""

        budget_tokens = 3_000
        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider("local", "Local", "http://127.0.0.1:1234/v1", auth_type="none"))
            resources.save_model(ModelAlias("model", "local", "model-1", protocol="openai"))
            gateway = OpenAICompatibleGateway(resources)
            observed: list[int] = []
            rounds = 6

            def fake_request(provider, model, protocol, credential, messages, **kwargs):
                # Measure the payload at call time: the loop mutates the list.
                observed.append(estimate_tokens(messages))
                if len(observed) > rounds:
                    return {"choices": [{"message": {"content": '{"result":"done"}'}}]}
                return {"choices": [{"message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": f"call-{len(observed)}",
                        "type": "function",
                        "function": {"name": "zvec_grep_rg", "arguments": '{"pattern":"x"}'},
                    }],
                }}]}

            with mock.patch.object(gateway, "_request_json", side_effect=fake_request):
                output, events = gateway.complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool"),
                    context=ContextState(inputs={"project": {"default_model": "model"}}),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    # Each result is far larger than the budget on its own.
                    tool_executor=lambda _name, _arguments: {"output": "hit " * 3_000},
                    max_rounds=rounds,
                    transcript_budget_tokens=budget_tokens,
                )

        self.assertEqual(output["result"], "done")
        self.assertEqual(len(events), rounds)
        self.assertGreater(len(observed), rounds)
        for index, tokens in enumerate(observed):
            self.assertLessEqual(
                tokens,
                budget_tokens,
                f"round {index + 1} request exceeded the transcript budget",
            )

    def test_exhausted_tool_rounds_return_evidence_instead_of_raising(self):
        """Hitting the round cap must not discard the accumulated work."""

        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider("local", "Local", "http://127.0.0.1:1234/v1", auth_type="none"))
            resources.save_model(ModelAlias("model", "local", "model-1", protocol="openai"))
            gateway = OpenAICompatibleGateway(resources)
            sent_tools: list[bool] = []

            def fake_request(provider, model, protocol, credential, messages, **kwargs):
                sent_tools.append(bool(kwargs.get("tools")))
                if not kwargs.get("tools"):
                    return {"choices": [{"message": {"content": '{"result":"partial"}'}}]}
                return {"choices": [{"message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call-x",
                        "type": "function",
                        "function": {"name": "zvec_grep_rg", "arguments": '{"pattern":"x"}'},
                    }],
                }}]}

            with mock.patch.object(gateway, "_request_json", side_effect=fake_request):
                output, events = gateway.complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool"),
                    context=ContextState(inputs={"project": {"default_model": "model"}}),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    tool_executor=lambda _name, _arguments: {"output": "evidence"},
                    max_rounds=2,
                )

        self.assertEqual(output["result"], "partial")
        self.assertTrue(output["tool_rounds_exhausted"])
        self.assertEqual(len(events), 2)
        # Two tool-enabled rounds, then one final request with tools withdrawn.
        self.assertEqual(sent_tools, [True, True, False])

    def test_claude_transcript_trimming_keeps_roles_alternating(self):
        """Claude rejects consecutive user turns and orphaned tool results."""

        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider(
                "local", "Local", "http://127.0.0.1:1234", protocols=["claude"], auth_type="none",
            ))
            resources.save_model(ModelAlias("model", "local", "claude-1", protocol="claude"))
            gateway = OpenAICompatibleGateway(resources)
            seen: list[list[dict]] = []

            def fake_request(provider, model, protocol, credential, messages, **kwargs):
                seen.append(copy.deepcopy(messages))
                if len(seen) > 4:
                    return {"content": [{"type": "text", "text": '{"result":"done"}'}]}
                return {"content": [{
                    "type": "tool_use",
                    "id": f"call-{len(seen)}",
                    "name": "zvec_grep_rg",
                    "input": {"pattern": "x"},
                }]}

            with mock.patch.object(gateway, "_request_json", side_effect=fake_request):
                gateway.complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool"),
                    context=ContextState(),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    tool_executor=lambda _name, _arguments: {"output": "hit " * 2_000},
                    max_rounds=4,
                    transcript_budget_tokens=2_500,
                )

        for index, messages in enumerate(seen):
            conversation = [item for item in messages if item.get("role") != "system"]
            roles = [item["role"] for item in conversation]
            for position in range(1, len(roles)):
                self.assertNotEqual(
                    roles[position],
                    roles[position - 1],
                    f"request {index + 1} has consecutive {roles[position]} turns: {roles}",
                )
            # Every tool_result block must follow an assistant tool_use block.
            for position, item in enumerate(conversation):
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    self.assertGreater(position, 0)
                    previous = conversation[position - 1]
                    self.assertEqual(previous.get("role"), "assistant")

    def test_claude_tool_loop_uses_assistant_blocks_and_tool_result_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider("local", "Local", "http://127.0.0.1:1234", protocols=["claude"], auth_type="none"))
            resources.save_model(ModelAlias("model", "local", "claude-1", protocol="claude"))
            gateway = OpenAICompatibleGateway(resources)
            responses = [
                {"content": [{"type": "tool_use", "id": "call-1", "name": "zvec_grep_rg", "input": {"pattern": "needle"}}]},
                {"content": [{"type": "text", "text": '{"result":"done"}'}]},
            ]
            with mock.patch.object(gateway, "_request_json", side_effect=responses) as request:
                output, _ = gateway.complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool"),
                    context=ContextState(),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    tool_executor=lambda _name, _arguments: {"output": "match"},
                )
            self.assertEqual(output["result"], "done")
            second_messages = request.call_args_list[1].args[4]
            self.assertEqual(second_messages[-1]["role"], "user")
            self.assertEqual(second_messages[-1]["content"][0]["type"], "tool_result")


class StreamingBudgetTest(unittest.TestCase):
    """The streaming loop must honour the same budget as the blocking loop."""

    class _Sse:
        def __init__(self, *records: bytes):
            self.lines = list(records)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def __iter__(self):
            return iter(self.lines)

        def read(self, *_args):
            return b"".join(self.lines)

    def _gateway(self, tmp: str, protocol: str) -> OpenAICompatibleGateway:
        resources = ResourceCenter(Path(tmp) / "resources")
        resources.save_provider(
            ModelProvider("local", "Local", "http://127.0.0.1:1234/v1", protocols=[protocol], auth_type="none")
        )
        resources.save_model(
            ModelAlias("model", "local", "model-1", protocol=protocol, context_window_tokens=200_000)
        )
        return OpenAICompatibleGateway(resources)

    def _final_packets(self, protocol: str) -> list[bytes]:
        if protocol == "claude":
            return [
                b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n',
                b'data: {"type":"message_stop"}\n\n',
            ]
        return [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b"data: [DONE]\n\n"]

    def test_streaming_requests_send_the_compaction_output_reserve(self):
        """A reserve on the node must reach the wire, not just the input budget."""

        for protocol in ("openai", "claude"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as tmp:
                gateway = self._gateway(tmp, protocol)
                payloads: list[dict] = []

                def capture(request, timeout=None):
                    payloads.append(json.loads(request.data.decode("utf-8")))
                    return self._Sse(*self._final_packets(protocol))

                node = WorkflowNode(
                    "chat", "tool", config={"_reserve_tokens": 777, "_stream_plain_text": True}
                )
                with mock.patch("urllib.request.urlopen", capture):
                    events = list(gateway.stream_complete_with_tools(
                        model_alias="model",
                        node=node,
                        context=ContextState(),
                        tools=[],
                        tool_executor=None,
                        transcript_budget_tokens=50_000,
                    ))

                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(
                    payloads[0].get("max_tokens"),
                    777,
                    f"{protocol} streaming ignored the compaction output reserve",
                )

    def test_streaming_tool_loop_keeps_every_request_within_the_budget(self):
        """The transcript grows each round; the streaming loop must re-clip it."""

        budget_tokens = 3_000
        rounds = 5
        with tempfile.TemporaryDirectory() as tmp:
            gateway = self._gateway(tmp, "openai")
            observed: list[int] = []

            def fake_stream(provider, model, protocol, credential, messages, **kwargs):
                observed.append(estimate_tokens(messages))
                if len(observed) > rounds:
                    yield {"event": "", "data": {"choices": [{"delta": {"content": "done"}}]}}
                    return
                yield {"event": "", "data": {"choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": f"call-{len(observed)}",
                    "function": {"name": "zvec_grep_rg", "arguments": '{"pattern":"x"}'},
                }]}}]}}

            with mock.patch.object(gateway, "_request_stream", side_effect=fake_stream):
                events = list(gateway.stream_complete_with_tools(
                    model_alias="model",
                    node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                    context=ContextState(inputs={"project": {"default_model": "model"}}),
                    tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                    tool_executor=lambda _name, _arguments: {"output": "hit " * 3_000},
                    max_rounds=rounds,
                    transcript_budget_tokens=budget_tokens,
                ))

        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1]["output"].get("tool_rounds_exhausted"))
        self.assertGreater(len(observed), rounds)
        for index, tokens in enumerate(observed):
            self.assertLessEqual(
                tokens,
                budget_tokens,
                f"streaming round {index + 1} request exceeded the transcript budget",
            )


class InvocationPolicyTest(unittest.TestCase):
    def test_workbench_reports_search_unavailable_without_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp) / "data")
            project = service.create_project("no workspace")
            status = service.search_status(project.project_id)
            self.assertFalse(status["ready"])
            self.assertTrue(status["local_only"])

    def test_runtime_policy_rejects_remote_embedding(self):
        from app.domain.models import Project

        with self.assertRaisesRegex(ValueError, "allow_remote_embedding"):
            Project.create(
                "remote policy",
                runtime_policy={
                    "local_search": {
                        "enabled": True,
                        "local_only": False,
                        "allow_remote_embedding": True,
                    }
                },
            )

    def test_tools_are_not_offered_when_the_local_backend_is_missing(self):
        """A missing zg must not cost one failed model round per attempt."""

        class FakeGateway:
            def __init__(self):
                self.tool_calls = 0
                self.plain_calls = 0

            def complete(self, **kwargs):
                self.plain_calls += 1
                return {"result": "ok"}

            def complete_with_tools(self, **kwargs):
                self.tool_calls += 1
                return {"result": "unexpected"}, []

        with tempfile.TemporaryDirectory() as tmp:
            gateway = FakeGateway()
            client = ZvecGrepClient()
            service = ModelInvocationService(gateway, search_client=client)
            context = ContextState(inputs={"project": {
                "workspace_path": str(Path(tmp).resolve()),
                "runtime_policy": default_runtime_policy(),
            }})
            with mock.patch.object(client, "available", return_value=False):
                service.invoke(
                    session=Session.create("project", "chat"),
                    node=WorkflowNode("chat", "tool"),
                    context=context,
                    output_fields=("result",),
                )
            self.assertEqual(gateway.tool_calls, 0)
            self.assertEqual(gateway.plain_calls, 1)

            with mock.patch.object(client, "available", return_value=True):
                service.invoke(
                    session=Session.create("project", "chat"),
                    node=WorkflowNode("chat", "tool"),
                    context=context,
                    output_fields=("result",),
                )
            self.assertEqual(gateway.tool_calls, 1)

    def test_summary_model_requires_an_explicit_summarization_capability(self):
        """No order-dependent fallback to an arbitrary enabled model."""

        with tempfile.TemporaryDirectory() as tmp:
            resources = ResourceCenter(Path(tmp) / "resources")
            resources.save_provider(ModelProvider("local", "Local", "http://127.0.0.1:1234/v1", auth_type="none"))
            resources.save_model(ModelAlias("main", "local", "model-1", protocol="openai"))
            gateway = OpenAICompatibleGateway(resources)
            service = ModelInvocationService(gateway)
            self.assertEqual(service._summary_alias(), "")

            resources.save_model(ModelAlias(
                "small", "local", "model-mini", protocol="openai", capabilities=["summarization"],
            ))
            self.assertEqual(service._summary_alias(), "small")

    def test_empty_node_tool_override_disables_project_search_defaults(self):
        class FakeGateway:
            def __init__(self):
                self.complete_calls = 0
                self.tool_calls = 0

            def complete(self, **kwargs):
                self.complete_calls += 1
                return {"result": "ok"}

            def complete_with_tools(self, **kwargs):
                self.tool_calls += 1
                return {"result": "unexpected"}, []

        gateway = FakeGateway()
        service = ModelInvocationService(gateway)
        session = Session.create("project", "chat")
        context = ContextState(inputs={
            "project": {
                "workspace_path": "",
                "runtime_policy": {
                    "local_search": {"enabled": True, "tools": ["zvec_grep_rg"]}
                },
            }
        })
        output = service.invoke(
            session=session,
            node=WorkflowNode("chat", "tool", config={"tools": []}),
            context=context,
            output_fields=("result",),
        )
        self.assertEqual(output["result"], "ok")
        self.assertEqual(gateway.complete_calls, 1)
        self.assertEqual(gateway.tool_calls, 0)


if __name__ == "__main__":
    unittest.main()
