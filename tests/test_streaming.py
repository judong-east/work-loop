from __future__ import annotations

import json
import io
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from threading import Thread
from typing import Any
from unittest import mock

from app.application.workbench import WorkbenchService
from app.domain.models import ContextState, ModelAlias, ModelProvider, SessionMode, WorkflowNode
from app.domain.tooling import SEARCH_TOOLS
from app.infrastructure.model_gateway import OpenAICompatibleGateway
from app.infrastructure.resource_center import ResourceCenter
from app.web.server import make_server


class _SseResponse:
    def __init__(self, *records: str):
        self.lines = [record.encode("utf-8") for record in records]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        return iter(self.lines)

    def read(self, *_args):
        return b"".join(self.lines)


def _sse(data: dict, event: str = "") -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False)}\n\n"


class GatewayStreamingTest(unittest.TestCase):
    def _gateway(self, protocol: str = "openai") -> OpenAICompatibleGateway:
        root = Path(self.tmp.name) / "resources"
        resources = ResourceCenter(root)
        resources.save_provider(
            ModelProvider(
                "local",
                "Local",
                "http://127.0.0.1:1234/v1",
                protocols=[protocol],
                auth_type="none",
            )
        )
        resources.save_model(ModelAlias("model", "local", "model-1", protocol=protocol))
        return OpenAICompatibleGateway(resources)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_openai_sse_streams_text_and_sets_stream_request(self):
        gateway = self._gateway()
        response = _SseResponse(
            _sse({"choices": [{"delta": {"role": "assistant"}}]}),
            _sse({"choices": [{"delta": {"content": "你好，"}}]}),
            _sse({"choices": [{"delta": {"content": "世界"}}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "data: [DONE]\n\n",
        )
        with mock.patch("urllib.request.urlopen", return_value=response) as opener:
            events = list(gateway.stream_complete_with_tools(
                model_alias="model",
                node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                context=ContextState(),
            ))
        self.assertEqual(
            [item["text"] for item in events if item["type"] == "text_delta"],
            ["你好，", "世界"],
        )
        self.assertEqual(events[-1]["output"], {"result": "你好，世界", "model": "model"})
        request = opener.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8"))["stream"], True)
        self.assertEqual(request.headers["Accept"], "text/event-stream")

    def test_openai_sse_tool_arguments_are_buffered_before_execution(self):
        gateway = self._gateway()
        first = _SseResponse(
            _sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "zvec_grep_rg", "arguments": '{"pattern"'},
            }]}}]}),
            _sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": ':"needle"}'},
            }]}}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            "data: [DONE]\n\n",
        )
        second = _SseResponse(
            _sse({"choices": [{"delta": {"content": "完成"}}]}),
            "data: [DONE]\n\n",
        )
        calls: list[tuple[str, dict]] = []
        with mock.patch("urllib.request.urlopen", side_effect=[first, second]):
            events = list(gateway.stream_complete_with_tools(
                model_alias="model",
                node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                context=ContextState(),
                tools=[SEARCH_TOOLS["zvec_grep_rg"]],
                tool_executor=lambda name, args: calls.append((name, args)) or {"matches": 1},
            ))
        self.assertEqual(calls, [("zvec_grep_rg", {"pattern": "needle"})])
        self.assertTrue(any(item["type"] == "tool_call" for item in events))
        self.assertTrue(any(item["type"] == "tool_result" for item in events))
        self.assertEqual(events[-1]["output"]["result"], "完成")

    def test_non_streaming_local_openai_response_is_exposed_as_one_delta(self):
        gateway = self._gateway()
        response = _SseResponse(json.dumps({
            "choices": [{"message": {"content": "普通端点"}}],
        }, ensure_ascii=False))
        with mock.patch("urllib.request.urlopen", return_value=response):
            events = list(gateway.stream_complete_with_tools(
                model_alias="model",
                node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                context=ContextState(),
            ))
        self.assertEqual(events[-1]["output"]["result"], "普通端点")
        self.assertEqual([item["text"] for item in events if item["type"] == "text_delta"], ["普通端点"])

    def test_stream_rejection_retries_one_shot_json_for_local_endpoint(self):
        gateway = self._gateway()
        rejected = urllib.error.HTTPError(
            "http://127.0.0.1:1234/v1/chat/completions",
            400,
            "stream unsupported",
            {},
            io.BytesIO(b'{"error":"stream unsupported"}'),
        )
        response = _SseResponse(json.dumps({
            "choices": [{"message": {"content": "回退"}}],
        }, ensure_ascii=False))
        with mock.patch("urllib.request.urlopen", side_effect=[rejected, response]) as opener:
            events = list(gateway.stream_complete_with_tools(
                model_alias="model",
                node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                context=ContextState(),
            ))
        self.assertEqual(events[-1]["output"]["result"], "回退")
        self.assertEqual(opener.call_count, 2)

    def test_claude_sse_streams_text_and_uses_messages_stream(self):
        gateway = self._gateway("claude")
        response = _SseResponse(
            _sse({"type": "message_start", "message": {}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Claude"}}, "content_block_delta"),
            _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
            _sse({"type": "message_stop"}, "message_stop"),
        )
        with mock.patch("urllib.request.urlopen", return_value=response) as opener:
            events = list(gateway.stream_complete_with_tools(
                model_alias="model",
                node=WorkflowNode("chat", "tool", config={"_stream_plain_text": True}),
                context=ContextState(),
            ))
        self.assertEqual(events[-1]["output"]["result"], "Claude")
        request = opener.call_args.args[0]
        self.assertEqual(request.headers["Accept"], "text/event-stream")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["stream"], True)


class HttpStreamingTest(unittest.TestCase):
    def test_message_stream_returns_sse_and_persists_complete_assistant_message(self):
        class FakeGateway:
            def stream_complete_with_tools(self, **_kwargs):
                yield {"type": "text_delta", "text": "流式"}
                yield {"type": "text_delta", "text": "回答"}
                yield {"type": "done", "output": {"result": "流式回答", "model": "fake"}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = WorkbenchService(root / "service", gateway=FakeGateway())
            project = service.create_project("stream")
            session = service.create_session(project.project_id, "chat", mode=SessionMode.CHAT)
            server = make_server(root / "server", 0)
            server.workbench = service
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/v2/sessions/{session.session_id}/messages/stream",
                data=json.dumps({"content": "请回答"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                content_type = response.headers["Content-Type"]
            self.assertTrue(content_type.startswith("text/event-stream"))
            self.assertIn('event: text_delta\ndata: {"type": "text_delta", "text": "流式"}', body)
            self.assertIn("event: done", body)
            restored = service.get_session(session.session_id)
            self.assertEqual(restored.messages[-1].role, "assistant")
            self.assertEqual(restored.messages[-1].content, "流式回答")


class _JsonResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, *_args):
        return self.body


class LoopEquivalenceTest(unittest.TestCase):
    """The blocking and streaming tool loops must not be able to diverge.

    Both public entry points drive one shared loop body.  These tests pin the
    observable consequences of that sharing, so re-introducing a second loop --
    or fixing only one of them, which is how the streaming path once lost the
    compaction output reserve -- fails here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _gateway(self, protocol: str) -> OpenAICompatibleGateway:
        resources = ResourceCenter(Path(self.tmp.name) / f"resources-{protocol}")
        resources.save_provider(
            ModelProvider(
                "local", "Local", "http://127.0.0.1:1234/v1",
                protocols=[protocol], auth_type="none",
            )
        )
        resources.save_model(ModelAlias("model", "local", "model-1", protocol=protocol))
        return OpenAICompatibleGateway(resources)

    @staticmethod
    def _node() -> WorkflowNode:
        # No ``_stream_plain_text``: both paths must finalize through the same
        # JSON contract, which is what makes their outputs directly comparable.
        return WorkflowNode("chat", "tool", config={"_reserve_tokens": 512})

    @staticmethod
    def _executor(name: str, arguments: dict) -> dict:
        return {"route": "rg", "output": f"{name}:{arguments.get('pattern', '')}"}

    def _openai_blocking(self) -> list[_JsonResponse]:
        return [
            _JsonResponse({"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "zvec_grep_rg", "arguments": '{"pattern": "needle"}'},
                }],
            }}]}),
            _JsonResponse({"choices": [{"message": {
                "role": "assistant", "content": '{"result": "done"}',
            }}]}),
        ]

    def _openai_streaming(self) -> list[_SseResponse]:
        return [
            _SseResponse(
                _sse({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "c1",
                    "function": {"name": "zvec_grep_rg", "arguments": '{"pattern":'},
                }]}}]}),
                _sse({"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "function": {"arguments": ' "needle"}'},
                }]}}]}),
                "data: [DONE]\n\n",
            ),
            _SseResponse(
                _sse({"choices": [{"delta": {"content": '{"result": "done"}'}}]}),
                "data: [DONE]\n\n",
            ),
        ]

    def _claude_blocking(self) -> list[_JsonResponse]:
        return [
            _JsonResponse({"content": [{
                "type": "tool_use", "id": "c1", "name": "zvec_grep_rg",
                "input": {"pattern": "needle"},
            }]}),
            _JsonResponse({"content": [{"type": "text", "text": '{"result": "done"}'}]}),
        ]

    def _claude_streaming(self) -> list[_SseResponse]:
        return [
            _SseResponse(
                _sse({"index": 0, "content_block": {
                    "type": "tool_use", "id": "c1", "name": "zvec_grep_rg",
                }}, "content_block_start"),
                _sse({"index": 0, "delta": {
                    "type": "input_json_delta", "partial_json": '{"pattern": "needle"}',
                }}, "content_block_delta"),
                "data: [DONE]\n\n",
            ),
            _SseResponse(
                _sse({"index": 0, "content_block": {"type": "text", "text": ""}},
                     "content_block_start"),
                _sse({"index": 0, "delta": {"type": "text_delta", "text": '{"result": "done"}'}},
                     "content_block_delta"),
                "data: [DONE]\n\n",
            ),
        ]

    def test_both_paths_produce_the_same_output_and_tool_events(self):
        for protocol in ("openai", "claude"):
            with self.subTest(protocol=protocol):
                blocking = getattr(self, f"_{protocol}_blocking")()
                streaming = getattr(self, f"_{protocol}_streaming")()
                gateway = self._gateway(protocol)
                tools = [SEARCH_TOOLS["zvec_grep_rg"]]

                with mock.patch("urllib.request.urlopen", side_effect=blocking):
                    blocking_output, blocking_events = gateway.complete_with_tools(
                        model_alias="model", node=self._node(), context=ContextState(),
                        tools=tools, tool_executor=self._executor,
                    )

                with mock.patch("urllib.request.urlopen", side_effect=streaming):
                    events = list(gateway.stream_complete_with_tools(
                        model_alias="model", node=self._node(), context=ContextState(),
                        tools=tools, tool_executor=self._executor,
                    ))
                done = events[-1]
                self.assertEqual(done["type"], "done")
                self.assertEqual(blocking_output, done["output"])
                self.assertEqual(blocking_events, done["tool_events"])
                self.assertEqual(blocking_output.get("result"), "done")
                self.assertEqual(
                    [event["name"] for event in blocking_events], ["zvec_grep_rg"]
                )
                self.assertEqual(blocking_events[0]["arguments"], {"pattern": "needle"})

    def test_both_paths_send_the_same_output_reserve_on_every_request(self):
        for protocol in ("openai", "claude"):
            with self.subTest(protocol=protocol):
                tools = [SEARCH_TOOLS["zvec_grep_rg"]]
                sent: dict[str, list[Any]] = {}

                for mode, responses in (
                    ("blocking", getattr(self, f"_{protocol}_blocking")()),
                    ("streaming", getattr(self, f"_{protocol}_streaming")()),
                ):
                    gateway = self._gateway(protocol)
                    with mock.patch("urllib.request.urlopen", side_effect=responses) as opener:
                        if mode == "blocking":
                            gateway.complete_with_tools(
                                model_alias="model", node=self._node(), context=ContextState(),
                                tools=tools, tool_executor=self._executor,
                            )
                        else:
                            list(gateway.stream_complete_with_tools(
                                model_alias="model", node=self._node(), context=ContextState(),
                                tools=tools, tool_executor=self._executor,
                            ))
                    sent[mode] = [
                        json.loads(call.args[0].data.decode("utf-8")).get("max_tokens")
                        for call in opener.call_args_list
                    ]

                self.assertEqual(sent["blocking"], [512, 512])
                self.assertEqual(sent["streaming"], sent["blocking"])


if __name__ == "__main__":
    unittest.main()
